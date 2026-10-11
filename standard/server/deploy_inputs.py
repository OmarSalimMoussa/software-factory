"""What one deploy is given: its arguments, refused when they are not a deploy the command takes; the server's
paths; the tools it acts with, which a test replaces; and its log, printed and kept in the workspace's deploy log.
"""

from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from standard import config as config_mod
from standard.server import deploy_steps as steps
from standard.server import gc
from standard.server.deploy_steps import SHORT, Get
from standard.services import provision
from standard.services.provision import Run

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
    # telemetry: surfaced — a hook that runs too long comes back as a failed `CompletedProcess`
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", f"timed out after {provision.TIMEOUT_SECONDS} s")
    # telemetry: surfaced — a hook that cannot start comes back as a failed `CompletedProcess`
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
    product_toml: Path = Path("/etc/software-factory/product.toml")
    docker_socket: str = "/var/run/docker.sock"
    gc_log: Path = gc.LOG


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
                file.write(f"{stamp(self.utc())} {line}\n")


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


def stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
