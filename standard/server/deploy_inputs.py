"""What one deploy is given: its arguments, refused when they are not a deploy the command takes; the server's
paths; the tools it acts with, which a test replaces; and its log, printed and kept in prod's deploy log.
"""

from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from standard.server import deploy_steps as steps
from standard.server import edge, gc, heartbeat
from standard.server.deploy_steps import SHORT, Get
from standard.services import host_paths, provision
from standard.services.provision import Run

_COMMIT = re.compile(r"[0-9a-f]{40}")
BACK = "back"


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
    mirror: Path
    srv: Path = Path("/srv")  # each workspace lives in <srv>/<product>/<workspace>/
    lock: Path = Path("/srv/factory/state/deploy.lock")
    edge_sites: Path = edge.SITES
    product_toml: Path = heartbeat.PRODUCT_TOML
    docker_socket: str = host_paths.DOCKER_SOCKET
    gc_log: Path = gc.LOG


@dataclass(frozen=True, slots=True)
class Tools:
    run: Run = provision.real_run  # every docker call
    git: Run = provision.real_run
    get: Get = steps.http_get
    hook: Hook = run_hook
    now: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    utc: Callable[[], datetime] = utc_now


@dataclass(frozen=True, slots=True)
class Args:
    commit: str
    back: bool  # a push back: it deploys with the server's product.toml as it is

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
    if not argv or argv[1:] not in ([], [BACK]):
        raise Refused(f"name one commit, and {BACK} for a push back")
    if not _COMMIT.fullmatch(argv[0]):
        raise Refused(f"{argv[0]!r} is not a commit: give all 40 lowercase hex characters")
    return Args(argv[0], argv[1:] == [BACK])


def stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
