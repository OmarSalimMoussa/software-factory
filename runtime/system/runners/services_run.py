"""software-services.service: the laptop's shared services, on the stack user's rootless Docker.

Run as ``/usr/bin/python3 -I <install>/runtime/system/runners/services_run.py up|stop``. ``up`` starts every service
and waits until each is healthy, naming any that is not. ``stop`` stops them and keeps their containers, so each
slot's network connections are still there when they start again. Stdlib only.
"""

from __future__ import annotations

import sys
from pathlib import Path

# `python3 -I` leaves the script's own folder off sys.path; the install's root is root-owned, so it is safe to add.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from runtime.shared.paths import SERVICES
from standard.services import provision, stack


def main(argv: list[str]) -> int:
    folder = SERVICES
    if argv == ["up"]:
        try:
            stack.up(provision.real_run, folder, edge=False)
        # telemetry: surfaced — the failure is `print`ed to the unit's log, and the exit says so
        except (stack.StackError, ValueError, KeyError) as exc:
            print(f"services: {type(exc).__name__}: {exc}", flush=True)
            return 1
        return 0
    if argv == ["stop"]:
        done = provision.real_run([*stack.compose_argv(folder, edge=False), "stop"])
        if done.returncode != 0:
            print(f"services: the stop failed (exit {done.returncode}): {provision.tail(done.stderr)}", flush=True)
        return 0 if done.returncode == 0 else 1
    print("services: name up or stop", file=sys.stderr, flush=True)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
