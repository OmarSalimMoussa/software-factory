"""Docker calls for CI's smoke test, and a look at one running container: its state, and one HTTP request made from a
throwaway probe container that shares the container's network, so no product image needs any tool.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

IMAGE = "python:3.12-alpine"
SECONDS = 5

# One GET: prints the status (0 when nothing answered), a newline, then the body. A redirect is an answer, not followed.
SCRIPT = """
import sys, urllib.error, urllib.request
class Stay(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None
try:
    with urllib.request.build_opener(Stay).open(sys.argv[1], timeout=float(sys.argv[2])) as reply:
        status, body = reply.status, reply.read(65536)
except urllib.error.HTTPError as exc:
    status, body = exc.code, b""
except Exception:
    status, body = 0, b""
sys.stdout.write(f"{status}\\n" + body.decode("utf-8", "replace"))
"""


@dataclass(frozen=True, slots=True)
class Result:
    code: int
    out: str
    err: str


class Run(Protocol):
    def __call__(
        self, argv: list[str], *, env: Mapping[str, str] | None = None, cwd: Path | None = None, stream: bool = False
    ) -> Result: ...


def real_run(
    argv: list[str], *, env: Mapping[str, str] | None = None, cwd: Path | None = None, stream: bool = False
) -> Result:
    """No shell. ``stream`` hands the output straight to CI's log instead of capturing it."""
    if stream:
        return Result(subprocess.run(argv, env=env, cwd=cwd, check=False).returncode, "", "")
    done = subprocess.run(argv, env=env, cwd=cwd, check=False, capture_output=True, text=True)
    return Result(done.returncode, done.stdout, done.stderr)


@dataclass(frozen=True, slots=True)
class State:
    status: str
    exit_code: int
    restarts: int
    health: str | None  # None when the container has no healthcheck

    @classmethod
    def read(cls, inspected: str) -> State:
        """From ``docker inspect <container>``'s output."""
        found = json.loads(inspected)[0]
        state = found["State"]
        return cls(
            state["Status"],
            state["ExitCode"],
            found["RestartCount"],
            state["Health"]["Status"] if "Health" in state else None,
        )


def request(run: Run, container: str, port: int, path: str) -> tuple[int, str]:
    """The status (0 when nothing answered) and body of a GET of ``path`` on the container's own ``port``."""
    argv = ["docker", "run", "--rm", "--network", f"container:{container}", IMAGE, "python", "-c", SCRIPT]
    done = run([*argv, f"http://127.0.0.1:{port}{path}", str(SECONDS)])
    if done.code != 0:
        return 0, ""
    status, _, body = done.out.partition("\n")
    return int(status), body
