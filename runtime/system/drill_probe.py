#!/usr/bin/python3
"""A drill's probe: tries each target and prints, one line each, what happened. It never prints what it read.

    drill_probe.py --seconds N file:/path unix:/path tcp:host:port env:NAME ...

Each line reads ``<target> -> <outcome>``: opened, connected or set when the wall let it through; the errno's name
(EACCES, EPERM, ECONNREFUSED, ENOENT...) or unset when it did not. Stdlib only, so it runs inside any sandbox.
"""

from __future__ import annotations

import errno
import os
import socket
import sys

BREACHES = frozenset({"opened", "connected", "set"})


def probe(target: str, seconds: float) -> str:
    kind, _, rest = target.partition(":")
    try:
        if kind == "file":
            with open(rest, "rb"):
                return "opened"
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
        return errno.errorcode.get(exc.errno or 0, type(exc).__name__)
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
