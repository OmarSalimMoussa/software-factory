"""Pause the alerts before the laptop sleeps (the system-sleep hook, as root) or the warden stops (as the warden).

Run as ``/usr/bin/python3 -I <install>/runtime/system/pause_checks.py pre|post|stop``. Waking (``post``) needs
nothing: the next check-in resumes a paused check. ``stop`` is the warden's ExecStopPost, which systemd runs after
every exit, crashes included: only a stop someone asked for pauses, so a warden that crashes and restarts keeps the
alerts on and the owner hears of it. Stdlib only.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# `python3 -I` leaves the script's own folder off sys.path; the install's root is root-owned, so it is safe to add.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from runtime.shared import settings  # noqa: E402 - the path line above must run first
from runtime.shared.paths import Places  # noqa: E402
from runtime.system import healthchecks  # noqa: E402

PAUSING = ("pre", "stop")
# What systemd sets SERVICE_RESULT to when the service ended cleanly: a stop it was asked for. A crash reads
# exit-code, signal, core-dump, watchdog or timeout.
CLEAN = "success"


def main(argv: list[str], environ: dict[str, str] | None = None) -> int:
    environ = dict(os.environ) if environ is None else environ
    if not argv or argv[0] not in PAUSING:
        return 0
    if argv[0] == "stop" and environ.get("SERVICE_RESULT") != CLEAN:
        print(f"pause_checks: the warden ended by {environ.get('SERVICE_RESULT') or 'an unknown cause'}, not a stop; "
              "the alerts stay on", file=sys.stderr)  # fmt: skip
        return 0
    places = Places.system()
    try:
        effective = settings.load(places.defaults, places.overrides)
    except settings.SettingsError as exc:
        print(f"pause_checks: {exc}", file=sys.stderr)
        return 1
    problems = healthchecks.pause(places.state, effective)
    for problem in problems:
        print(f"pause_checks: could not pause {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
