#!/usr/bin/python3
"""A drill's probe: tries each target and prints, one line each, what happened. It never prints what it read.

    drill_probe.py --seconds N file:/path dir:/path write:/path unix:/path tcp:host:port env:NAME get:http://... ...

Each line reads ``<target> -> <outcome>``: opened, listed, written, connected or set when the wall let it through (a
write opens the file to append and writes nothing); the errno's
name (EACCES, EPERM, ECONNREFUSED, ENOENT...) or unset when it did not. A ``get`` is the one target meant to get
through: it answers with the HTTP status. Stdlib only, so it runs inside any sandbox.
"""

from __future__ import annotations

import errno
import os
import socket
import sys
import urllib.error
import urllib.request

BREACHES = frozenset({"opened", "listed", "written", "connected", "set"})


def _errno(exc: OSError) -> str:
    return errno.errorcode.get(exc.errno or 0, type(exc).__name__)


def _get(url: str, seconds: float) -> str:
    try:
        with urllib.request.urlopen(url, timeout=seconds) as answer:  # noqa: S310 -- the drill's own loopback URL
            return f"answered {answer.status}"
    except urllib.error.HTTPError as exc:
        return f"answered {exc.code}"
    except urllib.error.URLError as exc:
        return _errno(exc.reason) if isinstance(exc.reason, OSError) else type(exc).__name__


def probe(target: str, seconds: float) -> str:
    kind, _, rest = target.partition(":")
    try:
        if kind == "file":
            with open(rest, "rb"):
                return "opened"
        if kind == "dir":
            os.listdir(rest)
            return "listed"
        if kind == "write":
            with open(rest, "ab"):
                return "written"
        if kind == "get":
            return _get(rest, seconds)
        if kind == "unix":
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(seconds)
                sock.connect(rest)
                return "connected"
        if kind == "tcp":
            host, _, port = rest.rpartition(":")
            with socket.create_connection((host, int(port)), timeout=seconds):
                return "connected"
        if kind == "env":
            return "set" if os.environ.get(rest) else "unset"
    except OSError as exc:
        return _errno(exc)
    except ValueError:
        return "unreadable target"
    return "unknown kind"


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
