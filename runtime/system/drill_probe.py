#!/usr/bin/python3
"""A drill's probe: tries each target and prints, one line each, what happened. It never prints what it read.

    drill_probe.py --seconds N file:/path dir:/path write:/path unix:/path tcp:host:port env:NAME get:http://... ...

Each line reads ``<target> -> <outcome>``: opened, listed, written, connected or set when the wall let it through (a
write opens the file to append and writes nothing); the errno's name (EACCES, EPERM, ECONNREFUSED, ENOENT...), unset,
or empty when it did not: Claude Code's sandbox hides a file it denies by mounting /dev/null over it, which opens.
A ``get`` is the one target meant to get through: it answers with the HTTP status. Stdlib only, so it runs inside any
sandbox.
"""

from __future__ import annotations

import errno
import os
import socket
import sys
import urllib.error
import urllib.request
from collections.abc import Callable

BREACHES = frozenset({"opened", "listed", "written", "connected", "set"})


def _errno(exc: OSError) -> str:
    return errno.errorcode.get(exc.errno or 0, type(exc).__name__)


def _get(url: str, seconds: float) -> str:
    try:
        with urllib.request.urlopen(url, timeout=seconds) as answer:
            return f"answered {answer.status}"
    # telemetry: expected — an error status is an answer, the outcome this probe reports
    except urllib.error.HTTPError as exc:
        return f"answered {exc.code}"
    # telemetry: expected — a refused or unreachable address is the outcome this probe reports
    except urllib.error.URLError as exc:
        return _errno(exc.reason) if isinstance(exc.reason, OSError) else type(exc).__name__


def _opened(path: str, _: float) -> str:
    with open(path, "rb") as handle:
        return "opened" if handle.read(1) else "empty"


def _listed(path: str, _: float) -> str:
    os.listdir(path)
    return "listed"


def _written(path: str, _: float) -> str:
    with open(path, "ab"):
        return "written"


def _unix(path: str, seconds: float) -> str:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(seconds)
        sock.connect(path)
        return "connected"


def _tcp(address: str, seconds: float) -> str:
    host, _, port = address.rpartition(":")
    with socket.create_connection((host, int(port)), timeout=seconds):
        return "connected"


# Each kind of target, `<kind>:<rest>`: how to try it, from its rest and the seconds it may take.
_KINDS: dict[str, Callable[[str, float], str]] = {
    "file": _opened,
    "dir": _listed,
    "write": _written,
    "get": _get,
    "unix": _unix,
    "tcp": _tcp,
    "env": lambda name, _: "set" if os.environ.get(name) else "unset",
}


def probe(target: str, seconds: float) -> str:
    kind, _, rest = target.partition(":")
    reach = _KINDS.get(kind)
    if reach is None:
        return "unknown kind"
    try:
        return reach(rest, seconds)
    # telemetry: expected — a refusal by the wall is the outcome this probe reports
    except OSError as exc:
        return _errno(exc)
    # telemetry: surfaced — a target it cannot read comes back from `probe` as such, and the drill fails
    except ValueError:
        return "unreadable target"


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[0] != "--seconds":
        print("usage: drill_probe.py --seconds N target...", file=sys.stderr)
        return 2
    seconds = float(argv[1])
    for target in argv[2:]:
        print(f"{target} -> {probe(target, seconds)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
