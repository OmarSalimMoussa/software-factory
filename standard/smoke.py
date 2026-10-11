"""The smoke test CI runs on a commit's images before any deploy.

It starts the images as a scratch workspace beside a scratch Postgres, with every telemetry address on a dead port
(telemetry must fail open), waits for each built service, checks that the health address names this commit, runs the
product's own check, and always tears everything down. Every request comes from a throwaway probe container that
shares the target's network, so no product image needs any tool. Run on the CI runner after a registry login:
``python3 -m standard.smoke --repo . --commit <40-hex>``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shlex
import shutil
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from standard import config as settings
from standard import envfile, environment, probe, yaml_subset
from standard.config_records import Config, Expect
from standard.probe import Result, Run, State
from standard.services import provision, stack
from standard.services.provision import POSTGRES

WORKSPACE = "smoke"
DEAD = "http://127.0.0.1:9"  # the discard port: nothing listens, so every telemetry send fails
DOCKER_SOCKET = "/var/run/docker.sock"
POLL_SECONDS = 2
LOG_LINES = 50


class SmokeError(Exception):
    """The smoke test failed; ``logs`` names the compose services whose last lines explain it."""

    def __init__(self, message: str, logs: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.logs = logs


def health_problems(body: str, short: str) -> list[str]:
    """What a health answer lacks: JSON naming this commit, the smoke workspace and a whole-number ``busy``."""
    try:
        answer = json.loads(body)
    except ValueError:
        return [f"it is not JSON: {body[:200]!r}"]
    if not isinstance(answer, dict):
        return [f"it is not a JSON object: {body[:200]!r}"]
    problems = []
    if answer.get("build_commit") != short:
        problems.append(f"build_commit is {answer.get('build_commit')!r}, not this commit {short!r}")
    if answer.get("workspace") != WORKSPACE:
        problems.append(f"workspace is {answer.get('workspace')!r}, not {WORKSPACE!r}")
    busy = answer.get("busy")
    if isinstance(busy, bool) or not isinstance(busy, int):
        problems.append(f"busy is {busy!r}, not a whole number (deploys wait while it is above 0)")
    return problems


class Smoke:
    """One smoke run. Every docker call goes through ``run``; ``clock`` and ``sleep`` drive the waits."""

    def __init__(
        self,
        config: Config,
        repo: Path,
        commit: str,
        run: Run,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        say: Callable[[str], None] = print,
    ) -> None:
        self.config, self.repo, self.run, self.clock, self.sleep, self.say = config, repo, run, clock, sleep, say
        self.short = commit[:8]
        self.project = config.project(WORKSPACE)
        self.network = provision.network(config, WORKSPACE)
        self.telemetry_network = provision.telemetry_network(config, WORKSPACE)
        self.database = provision.database(config, WORKSPACE)
        self.timeout = config.smoke.timeout_seconds
        self.networks: list[str] = []
        self.postgres = False
        self.env_dir: Path | None = None
        self.link: Path | None = None
        self.compose: list[str] = []
        self.started = False
        self.built: list[str] = []

    def test(self) -> None:
        failure: SmokeError | None = None
        try:
            self._steps()
        except SmokeError as exc:
            failure = exc
            for service in exc.logs:
                self._print_logs(service)
        finally:
            problems = self._teardown()
            for problem in problems:
                self.say(f"smoke: teardown: {problem}")
        if failure is not None:
            raise failure
        if problems:
            raise SmokeError("the scratch stack could not be torn down: " + "; ".join(problems))
        self.say(f"smoke: PASS: {self.short} starts, answers as itself and passes its checks")

    def _steps(self) -> None:
        self.say(f"smoke: starting {self.short} as the scratch workspace {self.project}")
        # The teardown removes the container by this name, so it must be the scratch one, never shared services' own.
        if self.run(["docker", "container", "inspect", POSTGRES]).code == 0:
            raise SmokeError(f"a container named {POSTGRES} already runs here; the smoke test runs only on a CI runner")
        for network in (self.network, self.telemetry_network):
            self._docker(["docker", "network", "create", network], f"create the network {network}")
            self.networks.append(network)
        self._docker(["docker", "pull", "--quiet", probe.IMAGE], f"pull the probe image {probe.IMAGE}")
        password = secrets.token_hex(16)
        # The shared services' own Postgres, read from their compose file, so the smoke runs what the product runs on.
        image = yaml_subset.load(stack.COMPOSE.read_text())["services"]["postgres"]["image"]
        self.postgres = True
        self._docker(
            ["docker", "run", "-d", "--name", POSTGRES, "--network", self.network]
            + ["-e", f"POSTGRES_USER={self.database}", "-e", f"POSTGRES_PASSWORD={password}"]
            + ["-e", f"POSTGRES_DB={self.database}", image],
            f"start the scratch Postgres ({image})",
        )
        self._wait_postgres()
        env_file = self._write_env(password)
        self.compose = ["docker", "compose", "-p", self.project, "--env-file", str(env_file)]
        self.compose += ["-f", str(self.repo / "compose.yml")]
        self.built = self._read_compose()
        self.say(f"smoke: pulling and starting {', '.join(self.built)} at {self.short}")
        self.started = True
        self._docker(self.compose + ["pull", "--quiet", *self.built], "pull this commit's images")
        self._docker(self.compose + ["up", "-d", "--no-build", *self.built], "start the stack", tuple(self.built))
        ids = {service: self._container(service) for service in self.built}
        self._wait_ready(ids)
        self._check_health(ids)
        self._run_check(env_file)
        # Asked again by service: the product's check may have recreated a container, which gives it a new id.
        for service in self.built:
            self._alive(service, self._state(self._container(service)))

    def _docker(self, argv: list[str], what: str, logs: tuple[str, ...] = ()) -> Result:
        done = self.run(argv)
        if done.code != 0:
            said = (done.err.strip() or done.out.strip())[-2000:]
            raise SmokeError(f"could not {what} (exit code {done.code}): {said}", logs)
        return done

    def _wait_postgres(self) -> None:
        ready = ["docker", "exec", POSTGRES, "pg_isready", "-h", "127.0.0.1", "-U", self.database, "-d", self.database]

        def attempt() -> str:
            done = self.run(ready)
            return "" if done.code == 0 else f"the scratch Postgres: {(done.out + done.err).strip()}"

        self._until(attempt, ())

    def _until(self, attempt: Callable[[], str], logs: tuple[str, ...]) -> None:
        """Repeats ``attempt`` until it returns ""; at the timeout its last answer says what is still missing."""
        deadline = self.clock() + self.timeout
        while reason := attempt():
            if self.clock() >= deadline:
                raise SmokeError(f"not ready within {self.timeout}s: {reason}", logs)
            self.sleep(POLL_SECONDS)

    def _services(self, password: str) -> dict[str, str]:
        """The factory's env values for the smoke workspace (provision.env_values names them all), every telemetry
        address the discard port."""
        name, numbers = self.config.product.name, {e: n for n, e in enumerate(self.config.services.errors, 1)}
        public = self.config.services.errors_public_path
        return provision.env_values(
            self.config, workspace=WORKSPACE, commit=self.short, networks=(self.network, self.telemetry_network),
            database_url=f"postgresql://{self.database}:{password}@{POSTGRES}:5432/{self.database}",
            langfuse=(DEAD, "pk-lf-smoke", "sk-lf-smoke"), ingest=DEAD,
            dsn=lambda errors: f"http://{name}@127.0.0.1:9/{numbers[errors]}",
            public_dsn=lambda errors: f"http://{name}@127.0.0.1:9{public}/{numbers[errors]}",
        )  # fmt: skip

    def _write_env(self, password: str) -> Path:
        # Compose reads the env file next to compose.yml, as on a server, where it links into the env folder.
        link = self.repo / self.config.env.file
        if os.path.lexists(link):
            raise SmokeError(f"{self.config.env.file} already exists in the checkout; the smoke test writes its own")
        self.env_dir = Path(tempfile.mkdtemp(prefix="factory-smoke-"))
        target = self.env_dir / self.config.env.file
        port = self.config.workspace("staging").port
        placeholders = envfile.placeholder_values(
            workspace=WORKSPACE,
            origin=f"http://127.0.0.1:{port}",
            port=port,
            commit=self.short,
            dir=self.repo,
            env_dir=self.env_dir,
            docker_socket=DOCKER_SOCKET,
        )
        try:
            notes = envfile.write(
                self.config,
                template=self.repo / self.config.env.template,
                target=target,
                placeholders=placeholders,
                services=self._services(password),
                server=False,
            )
        except envfile.EnvError as exc:
            raise SmokeError(f"the env file cannot be written: {'; '.join(exc.problems)}") from exc
        for note in notes:
            self.say(f"smoke: env file: {note}")
        link.symlink_to(target)
        self.link = link
        return target

    def _read_compose(self) -> list[str]:
        """The built services, once each names this commit's image; creates the external networks compose names."""
        model: dict[str, Any] = json.loads(
            self._docker(self.compose + ["config", "--format", "json"], "read compose.yml").out
        )
        services: dict[str, Any] = model["services"]
        built = [name for name, service in services.items() if "build" in service]
        problems = []
        for name in built:
            wanted = self.config.image(name, self.short)
            if services[name].get("image") != wanted:
                problems.append(f"compose.yml's {name} must run the image CI built, {wanted}")
        wanted_by_smoke = [self.config.smoke.health.service] + [e.service for e in self.config.smoke.expect]
        for name in dict.fromkeys(wanted_by_smoke):
            if name not in built:
                problems.append(f"factory.toml's [smoke] names {name}, which compose.yml does not build")
        if problems:
            raise SmokeError("; ".join(problems))
        for network in model.get("networks", {}).values():
            if network.get("external") is True and self.run(["docker", "network", "inspect", network["name"]]).code:
                self._docker(["docker", "network", "create", network["name"]], f"create the network {network['name']}")
                self.networks.append(network["name"])
        return built

    def _container(self, service: str) -> str:
        ids = self._docker(
            self.compose + ["ps", "--all", "--quiet", service], f"find {service}", (service,)
        ).out.split()
        if len(ids) != 1:
            raise SmokeError(f"{service} should run as one container; compose has {len(ids)}", (service,))
        return ids[0]

    def _state(self, container: str) -> State:
        return State.read(self._docker(["docker", "inspect", container], f"inspect {container}").out)

    def _alive(self, service: str, state: State) -> None:
        if state.status != "running":
            raise SmokeError(f"{service} stopped ({state.status}, exit code {state.exit_code})", (service,))
        if state.restarts:
            raise SmokeError(f"{service} crashed and restarted {state.restarts} time(s)", (service,))
        if state.health == "unhealthy":
            raise SmokeError(f"{service} reports itself unhealthy", (service,))

    def _wait_ready(self, ids: dict[str, str]) -> None:
        """Healthy where there is a healthcheck, every [smoke].expect answered; all of them still running."""
        expects: dict[str, list[Expect]] = {s: [e for e in self.config.smoke.expect if e.service == s] for s in ids}
        waiting = dict.fromkeys(ids, "not checked yet")

        def attempt() -> str:
            for service, container in ids.items():
                state = self._state(container)
                self._alive(service, state)
                if service in waiting:
                    waiting[service] = self._unready(container, state, expects[service])
                    if not waiting[service]:
                        del waiting[service]
            return "; ".join(f"{service}: {reason}" for service, reason in waiting.items())

        self._until(attempt, tuple(ids))

    def _unready(self, container: str, state: State, expects: list[Expect]) -> str:
        """Why the service is not ready yet, or "" once it is; each answered expectation is struck off the list."""
        if state.health not in (None, "healthy"):
            return f"its healthcheck says {state.health}"
        while expects:
            status, _ = probe.request(self.run, container, expects[0].port, expects[0].path)
            if status != expects[0].status:
                said = f"answered {status}" if status else "did not answer"
                return f"{expects[0].path} on port {expects[0].port} {said}, not {expects[0].status}"
            expects.pop(0)
        return ""

    def _check_health(self, ids: dict[str, str]) -> None:
        service, port = self.config.smoke.health.service, self.config.smoke.health.port
        path = self.config.deploy.health
        body = ""

        def attempt() -> str:
            nonlocal body
            self._alive(service, self._state(ids[service]))
            status, body = probe.request(self.run, ids[service], port, path)
            said = f"answered {status}" if status else "did not answer"
            return "" if status == 200 else f"{service}'s {path} on port {port} {said}"

        self._until(attempt, (service,))
        problems = health_problems(body, self.short)
        if problems:
            raise SmokeError(f"{service}'s {path} answer: " + "; ".join(problems), (service,))
        self.say(f"smoke: {service} answers {path} as {self.short} in the {WORKSPACE} workspace")

    def _run_check(self, env_file: Path) -> None:
        check = self.config.smoke.check
        if not check:
            return
        self.say(f"smoke: running the product's check {check}")
        env = environment.handed_on()
        env.update(
            FACTORY_PROJECT=self.project,
            FACTORY_WORKSPACE=WORKSPACE,
            FACTORY_COMMIT=self.short,
            FACTORY_ENV_FILE=str(env_file),
            FACTORY_COMPOSE=shlex.join(self.compose),
            FACTORY_TIMEOUT_SECONDS=str(self.timeout),
        )
        done = self.run([str(self.repo / check)], env=env, cwd=self.repo, stream=True)
        if done.code != 0:
            raise SmokeError(f"the product's check {check} failed (exit code {done.code})", tuple(self.built))

    def _print_logs(self, service: str) -> None:
        done = self.run(self.compose + ["logs", "--no-color", "--tail", str(LOG_LINES), service])
        self.say(f"smoke: the last {LOG_LINES} lines of {service}:\n{done.out}{done.err}")

    def _teardown(self) -> list[str]:
        problems = []
        steps: list[list[str]] = []
        if self.started:
            steps.append(self.compose + ["down", "-v", "--remove-orphans"])
        if self.postgres:
            steps.append(["docker", "rm", "-f", "-v", POSTGRES])
        steps += [["docker", "network", "rm", network] for network in reversed(self.networks)]
        for argv in steps:
            done = self.run(argv)
            if done.code != 0:
                problems.append(f"`{shlex.join(argv[-4:])}` failed: {(done.err or done.out).strip()}")
        try:
            if self.link is not None:
                self.link.unlink()
            if self.env_dir is not None:
                shutil.rmtree(self.env_dir)
        except OSError as exc:
            problems.append(f"the scratch env file could not be removed: {exc}")
        return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m standard.smoke", description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--commit", required=True, help="the full 40-character commit CI built")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[0-9a-f]{40}", args.commit):
        parser.error("--commit must be a full commit: 40 lowercase hex characters")
    repo = args.repo.resolve()
    try:
        config = settings.load(repo)
        Smoke(config, repo, args.commit, probe.real_run, say=lambda line: print(line, flush=True)).test()
    except settings.ConfigError as exc:
        print("smoke: FAIL: factory.toml: " + "; ".join(exc.problems), file=sys.stderr)
        return 1
    except SmokeError as exc:
        print(f"smoke: FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
