"""software-errors.service: the factory's own GlitchTip, on its own user's rootless Docker.

Run as ``/usr/bin/python3 -I <install>/runtime/system/runners/errors_run.py up|stop``. ``up`` starts it and waits
until it is healthy; ``stop`` stops it and keeps its containers. It starts and stops on its own, whatever the shared
services are doing, so the warden's errors about their trouble still have somewhere to go. Stdlib only.
"""

from __future__ import annotations

import sys
from pathlib import Path

# `python3 -I` leaves the script's own folder off sys.path; the install's root is root-owned, so it is safe to add.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from runtime.shared.paths import ERRORS
from runtime.system.errors import instance
from standard.services import provision


def main(argv: list[str]) -> int:
    if argv == ["up"]:
        try:
            instance.up(provision.real_run, ERRORS)
        # telemetry: surfaced — the failure is `print`ed to the unit's log, and the exit says so
        except instance.ErrorsError as exc:
            print(f"errors: {exc}", flush=True)
            return 1
        return 0
    if argv == ["stop"]:
        done = provision.real_run([*instance.compose_argv(ERRORS), "stop"])
        if done.returncode != 0:
            print(f"errors: the factory's own GlitchTip did not stop (exit {done.returncode}): "
                  f"{provision.tail(done.stderr)}", flush=True)  # fmt: skip
        return 0 if done.returncode == 0 else 1
    print("errors: name up or stop", file=sys.stderr, flush=True)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
