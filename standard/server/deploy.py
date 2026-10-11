"""The deploy command: one workspace on the server to one commit.

    factory-deploy <workspace> <commit> [follow | pin <ref>]

CI runs it as the admin user through the ci user's forced command; the registry token arrives on standard input.
Staging needs follow or pin; prod takes neither. One deploy runs at a time on a server, and every line it prints
also goes to the workspace's deploy log. Exit 0: deployed, or a pinned staging ignored a follow; 1: failed; 2: refused
arguments. docs/deploy.md walks through the steps.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from standard import compose as compose_mod
from standard import config as config_mod
from standard import envfile
from standard.compose import ComposeError
from standard.config import Config, ConfigError
from standard.envfile import EnvError, atomic_write
from standard.plan import DEPLOY_IMAGE
from standard.server import deploy_steps as steps
from standard.server import edge
from standard.server.deploy_steps import SHORT, DeployError, Get
from standard.services import provision
from standard.services.provision import ProvisionError, Run, checked

# The sections the server acts on: a commit that changes one needs the owner to run the server set-up again.
SERVER_SECTIONS = ("product", "services", "env", "server", "workspaces", "deploy", "edge")
USAGE = "usage: factory-deploy <staging|prod> <commit> [follow | pin <ref|->]"
_COMMIT = re.compile(r"[0-9a-f]{8,40}")
_REF = re.compile(r"-|[A-Za-z0-9._/][A-Za-z0-9._/-]{0,99}")

Hook = Callable[[list[str], Path, dict[str, str]], subprocess.CompletedProcess[str]]


class Refused(Exception):
    """The arguments are not a deploy this command takes."""


def run_hook(argv: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=provision.TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", f"timed out after {provision.TIMEOUT_SECONDS} s")
    except OSError as exc:
        return subprocess.CompletedProcess(argv, 126, "", f"cannot run it: {exc}")


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Paths:
    srv: Path = Path("/srv")  # each workspace lives in <srv>/<product>/<workspace>/
    lock: Path = Path("/srv/factory/state/deploy.lock")
    services_env: Path = Path("/srv/factory/services/services.env")
    edge_sites: Path = Path("/srv/factory/edge/sites")
    factory_commit: Path = Path("/usr/local/lib/software-factory/COMMIT")
    product_toml: Path = Path("/etc/software-factory/product.toml")
    docker_socket: str = "/var/run/docker.sock"


@dataclass(frozen=True, slots=True)
class Tools:
    run: Run = provision.real_run  # every docker call
    get: Get = steps.http_get
    hook: Hook = run_hook
    now: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    utc: Callable[[], datetime] = utc_now


@dataclass(frozen=True, slots=True)
class Args:
    workspace: str
    commit: str
    mode: str | None  # "follow", "pin <ref>", or None for prod

    @property
    def short(self) -> str:
        return self.commit[:SHORT]


@dataclass(frozen=True, slots=True)
class Log:
    path: Path
    utc: Callable[[], datetime]

    def __call__(self, text: str) -> None:
        for line in text.splitlines() or [""]:
            print(line, flush=True)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as file:
                file.write(f"{_stamp(self.utc())} {line}\n")


def parse_args(argv: list[str]) -> Args:
    if len(argv) < 2 or argv[0] not in config_mod.WORKSPACES:
        raise Refused("name the workspace (staging or prod) and the commit")
    workspace, commit, rest = argv[0], argv[1], argv[2:]
    if not _COMMIT.fullmatch(commit):
        raise Refused(f"{commit!r} is not a commit: give 8 to 40 lowercase hex characters")
    if workspace == "prod":
        if rest:
            raise Refused("prod takes no follow or pin")
        return Args(workspace, commit, None)
    if rest == ["follow"]:
        return Args(workspace, commit, "follow")
    if len(rest) == 2 and rest[0] == "pin" and _REF.fullmatch(rest[1]):
        return Args(workspace, commit, f"pin {rest[1]}")
    raise Refused("staging needs follow, or pin with a ref (or - to follow the default branch again)")


def main(argv: list[str], paths: Paths | None = None, tools: Tools | None = None, stdin: TextIO | None = None) -> int:
    paths, tools = paths or Paths(), tools or Tools()
    try:
        args = parse_args(argv)
    except Refused as exc:
        print(f"factory-deploy: refused: {exc}\n{USAGE}", flush=True)
        return 2
    try:
        config = config_mod.parse(paths.product_toml)
    except ConfigError as exc:
        print(f"factory-deploy: the server's copy of factory.toml is unusable ({exc}): run the server set-up again")
        return 1
    root = paths.srv / config.product.name / args.workspace
    log = Log(root / "state" / "deploy.log", tools.utc)
    log(f"deploy {args.workspace} {args.short}" + (f" {args.mode}" if args.mode else ""))
    try:
        return _Deploy(config, args, paths, tools, log, root).run(stdin or sys.stdin)
    except (DeployError, ProvisionError, OSError) as exc:
        log(f"failed: {exc}")
    except EnvError as exc:
        log("failed:\n" + "\n".join(f"  {problem}" for problem in exc.problems))
    except (ConfigError, ComposeError) as exc:
        log(f"failed: {exc.path}:\n" + "\n".join(f"  {problem}" for problem in exc.problems))
    return 1


class _Deploy:
    def __init__(self, config: Config, args: Args, paths: Paths, tools: Tools, log: Log, root: Path) -> None:
        self.config, self.args, self.paths, self.tools, self.log = config, args, paths, tools, log
        self.ws = config.workspace(args.workspace)
        self.root, self.stack, self.env_dir, self.state = root, root / "stack", root / "env", root / "state"
        self.env_file = self.env_dir / config.env.file

    def run(self, stdin: TextIO) -> int:
        pinned = self.state / "pinned"
        if self.args.mode == "follow" and pinned.exists():
            self.log(f"staging is pinned to {pinned.read_text().strip()}: this follow changes nothing")
            return 0
        with _lock(self.paths.lock, self.log):
            for folder, mode in ((self.stack, 0o755), (self.env_dir, 0o700), (self.state, 0o755)):
                folder.mkdir(parents=True, exist_ok=True, mode=mode)
            token = stdin.read().strip()
            if not token:
                raise DeployError("no registry token arrived on standard input")
            host = self.config.product.registry.split("/", 1)[0]
            checked(self.tools.run, ["docker", "login", host, "-u", "github-actions", "--password-stdin"], token)
            try:
                done = self.deploy()
                if done == 0:
                    self.pin(pinned)
                return done
            finally:
                if self.tools.run(["docker", "logout", host], None).returncode != 0:
                    self.log(
                        f"warning: docker logout {host} failed; the token stays in docker's config till it expires"
                    )

    def pin(self, pinned: Path) -> None:
        """A run by hand pins staging to its ref, or lifts the pin, only once its deploy succeeded: staging is never
        pinned to a commit it does not run."""
        if self.args.mode == "pin -":
            pinned.unlink(missing_ok=True)
            self.log("staging follows the default branch again")
        elif self.args.mode is not None and self.args.mode.startswith("pin "):
            atomic_write(pinned, self.args.mode[4:] + "\n", 0o644)
            self.log(f"staging is pinned to {self.args.mode[4:]}")

    def deploy(self) -> int:
        short, run, log = self.args.short, self.tools.run, self.log
        commit, images, changed = self.deploy_files()
        values = self.settings()
        for ref in images:
            checked(run, ["docker", "pull", "-q", f"{ref}:{short}"])
        log(f"images: pulled {len(images)} at {short}")
        health = self.config.deploy.health
        steps.drain(f"http://127.0.0.1:{self.ws.port}{health}", self.config.deploy.drain_minutes, *self.waits())
        self.up(values, changed)
        edge.write_sites(self.config, self.paths.edge_sites, self.ws.name)
        edge.reload(run)
        log(f"edge: {self.ws.domain} goes to 127.0.0.1:{self.ws.port}")
        want = f"{self.ws.name} at {short}"
        steps.answer(f"{self.ws.origin}{health}", want, self.config.deploy.answer_seconds, *self.waits())
        failed = self.hooks()
        previous = self.record(commit)
        if failed:
            log(f"failed: {failed}; {self.ws.name} is live at {short}, and every image stays for a rollback")
            return 1
        steps.prune(run, self.config, self.root.parent, log)
        log(f"deployed {self.ws.name} {short} previous {previous[:SHORT] if previous else '-'}")
        return 0

    def waits(self) -> tuple[Get, Callable[[], float], Callable[[float], None], Callable[[str], None]]:
        return self.tools.get, self.tools.now, self.tools.sleep, self.log

    def deploy_files(self) -> tuple[str, list[str], set[str]]:
        """Copy the deploy image's files into the stack folder: the full commit, the plan's images, what changed."""
        run = self.tools.run
        image = self.config.image(DEPLOY_IMAGE, self.args.short)
        checked(run, ["docker", "pull", "-q", image])
        with tempfile.TemporaryDirectory(dir=self.root, prefix=".deploy-") as temp:
            incoming = Path(temp) / "deploy"
            container = checked(run, ["docker", "create", image, "/none"]).strip()
            try:
                checked(run, ["docker", "cp", f"{container}:/deploy", str(incoming)])
            finally:
                run(["docker", "rm", container], None)
            commit, images = self.check_image(incoming, image)
            changed = steps.sync(incoming, self.stack, keep={self.config.env.file})
        link, target = self.stack / self.config.env.file, f"../{self.env_dir.name}/{self.config.env.file}"
        if not link.is_symlink() or os.readlink(link) != target:
            temp_link = link.with_name(f".{link.name}.link")
            temp_link.unlink(missing_ok=True)
            os.symlink(target, temp_link)
            os.replace(temp_link, link)
        self.log(f"deploy files: {commit} in {self.stack} ({len(changed)} changed)")
        return commit, images, changed

    def check_image(self, incoming: Path, image: str) -> tuple[str, list[str]]:
        commit = (incoming / "COMMIT").read_text(encoding="utf-8").strip()
        if not commit.startswith(self.args.commit):
            raise DeployError(f"{image} holds commit {commit!r}, not {self.args.commit}: the tag names another build")
        try:
            plan = json.loads((incoming / "plan.json").read_text(encoding="utf-8"))
        except ValueError as exc:
            raise DeployError(f"{image}'s plan.json is not JSON: {exc}") from exc
        ours = self.paths.factory_commit.read_text(encoding="utf-8").strip()
        theirs = plan.get("factory_commit") if isinstance(plan, dict) else None
        if theirs != ours:
            raise DeployError(
                f"this commit was planned by factory {theirs!r}, and the server runs factory {ours!r}: "
                "run the server set-up again from the factory commit .github/factory-ref names"
            )
        images = plan.get("images")
        if not isinstance(images, list) or not all(
            isinstance(i, dict) and isinstance(i.get("ref"), str) for i in images
        ):
            raise DeployError(f"{image}'s plan.json has no list of images, each with a ref")
        shipped = config_mod.parse(incoming / config_mod.FILE)
        differ = [f"[{s}]" for s in SERVER_SECTIONS if shipped.raw.get(s) != self.config.raw.get(s)]
        if differ:
            raise DeployError(
                f"factory.toml at this commit differs from the server's copy in {', '.join(differ)}: "
                "run the server set-up again"
            )
        if (incoming / self.config.env.file).exists():
            raise DeployError(f"the deploy files hold {self.config.env.file}, the env file the factory writes")
        return commit, [str(i["ref"]) for i in images]

    def settings(self) -> dict[str, str]:
        """Provision the workspace's shared services, then write its env file; the env file's values."""
        services = provision.read_services_env(self.paths.services_env)
        values = provision.workspace(
            self.config,
            services,
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
        env = dict(os.environ)
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

    def record(self, commit: str) -> str | None:
        """Write what now runs and add it to the history; the commit that ran before, if any."""
        deployed = self.state / "deployed"
        previous = deployed.read_text(encoding="utf-8").strip() if deployed.exists() else None
        atomic_write(deployed, commit + "\n", 0o644)
        entry = {"at": _stamp(self.tools.utc()), "commit": commit, "previous": previous, "mode": self.args.mode}
        with (self.state / "history.jsonl").open("a", encoding="utf-8") as file:
            file.write(json.dumps(entry) + "\n")
        return previous


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextmanager
def _lock(path: Path, log: Log) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("another deploy is running on this server; waiting for it to end")
            fcntl.flock(handle, fcntl.LOCK_EX)
        yield


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
