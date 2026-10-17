"""Prod's deploy: one commit, built on the box from the warden's mirror, and prod put on it.

    main([<40-character commit>])          the owner's Push to prod
    main([<40-character commit>, "back"])  the owner's Push back

The deploy-run unit runs it as the box's admin user, for the warden (runtime/system/runners/deploy_run.py). A push to
prod takes its settings from the commit's factory.toml, and makes the server's product.toml that copy once prod runs
the commit; a push back deploys with product.toml as it is, since this factory may not parse an older commit's
factory.toml. The build gets what ``wait_seconds`` leaves after the drain and the answer wait, so the whole deploy fits
in it. One deploy runs at a time on a server, and every line it prints also goes to prod's deploy log; a failure's
line starts `failed:`.
Exit 0: deployed; 1: failed; 2: refused arguments. docs/deploy.md walks through the steps.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from standard import compose as compose_mod
from standard import config as config_mod
from standard import envfile, environment
from standard import plan as plan_mod
from standard.compose import ComposeError
from standard.config import ConfigError
from standard.config_records import Config
from standard.envfile import EnvError, atomic_write
from standard.plan import PlanError
from standard.server import build, edge, gc
from standard.server import deploy_steps as steps
from standard.server.deploy_inputs import BACK, Args, Log, Paths, Refused, Tools, parse_args, stamp
from standard.server.deploy_steps import SHORT, DeployError, Get
from standard.services import provision
from standard.services.provision import ProvisionError, Run

WORKSPACE = "prod"
USAGE = f"usage: deploy <40-character commit> [{BACK}]"


def main(argv: list[str], paths: Paths, wait_seconds: int, tools: Tools | None = None) -> int:
    tools = tools or Tools()
    try:
        args = parse_args(argv)
    # telemetry: surfaced — the refusal is `print`ed for the warden's push to show, and the deploy exits 2
    except Refused as exc:
        print(f"failed: the deploy was refused: {exc}\n{USAGE}", flush=True)
        return 2
    try:
        config, fresh = _settings(args, paths, tools.git)
    # telemetry: surfaced — the reason is `print`ed for the warden's push to show, and the deploy exits 1
    except DeployError as exc:
        print(f"failed: {exc}", flush=True)
        return 1
    started, root = tools.now(), paths.srv / config.product.name / WORKSPACE
    log = Log(root / "state" / "deploy.log", tools.utc)
    log(f"deploy {WORKSPACE} {args.short}" + (f" {BACK}" if args.back else ""))
    try:
        return _Deploy(config, args, paths, tools, log, root).run(fresh, started + wait_seconds)
    # telemetry: surfaced — the failure goes to the deploy's `log`, which the warden's push shows and the server keeps
    except (DeployError, ProvisionError, OSError, EnvError, PlanError, ConfigError, ComposeError) as exc:
        log(f"failed: {exc}")
    return 1


def _settings(args: Args, paths: Paths, git: Run) -> tuple[Config, str | None]:
    """The settings the deploy runs with, and for a push to prod the factory.toml that product.toml becomes."""
    if not build.holds(git, paths.mirror, args.commit):
        raise DeployError(f"the warden's mirror holds no commit {args.commit}")
    if args.back:
        try:
            return config_mod.parse(paths.product_toml), None
        except ConfigError as exc:
            raise DeployError(f"the server's factory.toml does not parse ({exc}); a push to prod rewrites it") from exc
    text = build.read(git, paths.mirror, args.commit, config_mod.FILE)
    try:
        return config_mod.parse_text(text, Path(f"{config_mod.FILE} at {args.short}")), text
    except ConfigError as exc:
        raise DeployError(f"{exc}; prod stays on the build it runs") from exc


class _Deploy:
    def __init__(self, config: Config, args: Args, paths: Paths, tools: Tools, log: Log, root: Path) -> None:
        self.config, self.args, self.paths, self.tools, self.log = config, args, paths, tools, log
        self.ws = config.workspace(WORKSPACE)
        self.root, self.stack, self.env_dir, self.state = root, root / "stack", root / "env", root / "state"
        self.env_file = self.env_dir / config.env.file

    def run(self, fresh: str | None, deadline: float) -> int:
        with _lock(self.paths.lock, self.log):
            # The server has no mail: a failed weekly clean-up shows in every deploy's log until one passes.
            failed = gc.last_failure(self.paths.gc_log)
            if failed is not None:
                self.log(f"warning: the weekly clean-up failed ({failed}); the server's disk fills until it passes")
            for folder, mode in ((self.stack, 0o755), (self.env_dir, 0o700), (self.state, 0o755)):
                folder.mkdir(parents=True, exist_ok=True, mode=mode)
            target = self.paths.product_toml
            if fresh is not None and self.stale(fresh) and not os.access(target, os.W_OK):
                why = "as the server's set-up makes it: chown it to the admin user; prod stays on the build it runs"
                raise DeployError(f"{target} is not this user's to rewrite, {why}")
            return self.deploy(fresh, deadline)

    def stale(self, text: str) -> bool:
        target = self.paths.product_toml
        return not target.exists() or target.read_text(encoding="utf-8") != text

    def refresh(self, text: str) -> None:
        if not self.stale(text):
            return
        # In place: its folder is root's, and the server set-up gave the admin user this one file.
        with self.paths.product_toml.open("w", encoding="utf-8") as file:
            file.write(text)
        self.log(f"settings: {self.paths.product_toml} is now {config_mod.FILE} at {self.args.short}")

    def deploy(self, fresh: str | None, deadline: float) -> int:
        short, run, log = self.args.short, self.tools.run, self.log
        # Every image is built before the stack's files change: a build that fails leaves prod as it was.
        with tempfile.TemporaryDirectory(dir=self.root, prefix=".deploy-") as temp:
            source = build.unpack(self.tools.git, self.paths.mirror, self.args.commit, Path(temp))
            plan = plan_mod.make(source, self.config, self.args.commit)
            build.images(run, plan, source, log)
            switch = self.config.deploy.drain_minutes * 60 + self.config.deploy.answer_seconds
            if self.tools.now() + switch > deadline:
                raise DeployError(
                    "the build took too long to leave time for the drain and the answer wait in "
                    "watch.push_wait_minutes; prod stays on the build it runs"
                )
            changed = self.deploy_files(source, plan.files, Path(temp) / "deploy")
        values = self.settings()
        health = self.config.deploy.health
        steps.drain(f"http://127.0.0.1:{self.ws.port}{health}", self.config.deploy.drain_minutes, *self.waits())
        self.up(values, changed)
        edge.write_sites(self.config, self.paths.edge_sites, self.ws.name)
        edge.reload(run)
        log(f"edge: {self.ws.domain} goes to 127.0.0.1:{self.ws.port}")
        want = f"{self.ws.name} at {short}"
        steps.answer(f"{self.ws.origin}{health}", want, self.config.deploy.answer_seconds, *self.waits())
        failed = self.hooks()
        previous = self.record()
        if fresh is not None:
            self.refresh(fresh)
        if failed:
            log(f"failed: {failed}; {self.ws.name} is live at {short}, and every image stays for a rollback")
            return 1
        steps.prune(run, self.config, self.root.parent, log)
        log(f"deployed {self.ws.name} {short} previous {previous[:SHORT] if previous else '-'}")
        return 0

    def waits(self) -> tuple[Get, Callable[[], float], Callable[[float], None], Callable[[str], None]]:
        return self.tools.get, self.tools.now, self.tools.sleep, self.log

    def deploy_files(self, source: Path, files: tuple[str, ...], incoming: Path) -> set[str]:
        for rel in files:
            found, copy = source / rel, incoming / rel
            if not found.exists() and not found.is_symlink():
                raise DeployError(f"{rel}: the plan names it, but the commit has no such file or folder")
            copy.parent.mkdir(parents=True, exist_ok=True)
            if found.is_dir() and not found.is_symlink():
                shutil.copytree(found, copy, symlinks=True, dirs_exist_ok=True)
            else:
                shutil.copy2(found, copy, follow_symlinks=False)
        if (incoming / self.config.env.file).exists():
            raise DeployError(f"the deploy files hold {self.config.env.file}, the env file the factory writes")
        changed = steps.sync(incoming, self.stack, keep={self.config.env.file})
        link, target = self.stack / self.config.env.file, f"../{self.env_dir.name}/{self.config.env.file}"
        if not link.is_symlink() or os.readlink(link) != target:
            temp_link = link.with_name(f".{link.name}.link")
            temp_link.unlink(missing_ok=True)
            os.symlink(target, temp_link)
            os.replace(temp_link, link)
        self.log(f"deploy files: {self.args.commit} in {self.stack} ({len(changed)} changed)")
        return changed

    def settings(self) -> dict[str, str]:
        """Provision the workspace's shared services, then write its env file; the env file's values."""
        values = provision.workspace(
            self.config,
            self.tools.run,
            workspace=self.ws.name,
            origin=self.ws.origin,
            commit=self.args.short,
            state=self.state,
        )
        self.log(
            f"shared services: database {provision.database(self.config, self.ws.name)}, "
            f"{len(self.config.services.errors)} error projects, network {values['FACTORY_SERVICES_NETWORK']}"
        )
        placeholders = envfile.placeholder_values(
            workspace=self.ws.name,
            origin=self.ws.origin,
            port=self.ws.port,
            commit=self.args.short,
            dir=self.stack,
            env_dir=self.env_dir,
            docker_socket=self.paths.docker_socket,
        )
        template = self.stack / self.config.env.template
        notes = envfile.write(
            self.config,
            template=template,
            target=self.env_file,
            placeholders=placeholders,
            services=values,
            server=True,
        )
        for note in notes:
            self.log(f"env file: {note}")
        written, problems = envfile.parse_values(self.env_file.read_text(encoding="utf-8"), str(self.env_file))
        if problems:
            raise EnvError(problems)
        return written

    def up(self, values: dict[str, str], changed: set[str]) -> None:
        compose_file = self.stack / compose_mod.FILE
        model = compose_mod.read(compose_file)
        present = provision.networks(self.tools.run)
        for key, net in model.networks.items():
            if net.external:
                name = steps.resolve(net.name or key, values)
                if provision.ensure_network(self.tools.run, name, present):
                    self.log(f"stack: created network {name}")
                present.add(name)
        base = ["docker", "compose", "-p", self.config.project(self.ws.name), "--project-directory", str(self.stack)]
        base += ["--env-file", str(self.env_file), "-f", str(compose_file)]
        self._compose(base + ["up", "-d", "--remove-orphans", "--no-build", "--pull", "never"], "up")
        # A service reads a bind-mounted file only when it starts, and `up` recreates it only when its own entry
        # changed: one whose mounted file this deploy changed restarts, so the new file takes effect.
        stale = [s.name for s in model.services.values() if steps.touched(s.binds, changed)]
        if stale:
            self._compose(base + ["restart", *stale], "restart")
            self.log(f"stack: restarted {', '.join(stale)}: a file each mounts changed")

    def _compose(self, argv: list[str], what: str) -> None:
        done = self.tools.run(argv, None)
        for line in (done.stdout + done.stderr).splitlines():
            self.log(f"compose: {line}")
        if done.returncode != 0:
            raise DeployError(f"docker compose {what} failed (exit {done.returncode})")

    def hooks(self) -> str:
        """Run the workspace's after-deploy hooks in order; the first failure, or "" when all passed."""
        env = environment.handed_on()
        env.update(
            FACTORY_WORKSPACE=self.ws.name,
            FACTORY_COMMIT=self.args.short,
            FACTORY_STACK_DIR=str(self.stack),
            FACTORY_ENV_FILE=str(self.env_file),
            FACTORY_PROJECT=self.config.project(self.ws.name),
            FACTORY_REGISTRY=self.config.product.registry,
            FACTORY_PRODUCT=self.config.product.name,
        )
        for hook in self.ws.after_deploy:
            done = self.tools.hook([str(self.stack / hook)], self.stack, env)
            for line in (done.stdout + done.stderr).splitlines():
                self.log(f"{hook}: {line}")
            if done.returncode != 0:
                return f"after-deploy hook {hook} failed (exit {done.returncode})"
        return ""

    def record(self) -> str | None:
        """Write what now runs and add it to the history; the commit that ran before, if any."""
        deployed, commit = self.state / "deployed", self.args.commit
        previous = deployed.read_text(encoding="utf-8").strip() if deployed.exists() else None
        atomic_write(deployed, commit + "\n", 0o644)
        entry = {"at": stamp(self.tools.utc()), "commit": commit, "previous": previous, "back": self.args.back}
        with (self.state / "history.jsonl").open("a", encoding="utf-8") as file:
            file.write(json.dumps(entry) + "\n")
        return previous


@contextmanager
def _lock(path: Path, log: Log) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # telemetry: expected — another deploy holds the lock: this one says so in its log and waits its turn
        except BlockingIOError:
            log("another deploy is running on this server; waiting for it to end")
            fcntl.flock(handle, fcntl.LOCK_EX)
        yield
