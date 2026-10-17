"""software-deploy-run@<id>.service: one deploy of prod, as the box's admin user, on this machine's own Docker.

Run as ``/usr/bin/python3 -I <install>/runtime/system/runners/deploy_run.py <state> <id>``. The request names one full
commit and whether it is a push back; the deploy builds that commit from the warden's mirror and puts prod on it
(standard/server/deploy.py) within watch.push_wait_minutes. The unit's exit status is the deploy's. Stdlib only.
"""

from __future__ import annotations

import sys
from pathlib import Path

# `python3 -I` leaves the script's own folder off sys.path; the install's root is root-owned, so it is safe to add.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from runtime.shared.paths import Places
from runtime.system.runners import launch_file as lf
from standard.server import deploy
from standard.server.deploy_inputs import Paths


def main(argv: list[str]) -> int:
    state, request_id = Path(argv[0]), argv[1]
    try:
        words = lf.deploy_argv(lf.read(state, request_id, (lf.DEPLOY,)))
        wait = lf.published(state)[0].get_int("watch.push_wait_minutes") * 60
    # telemetry: surfaced — the refusal is `print`ed to the unit's log, and the exit says it was refused
    except lf.BadRequest as exc:
        print(f"failed: deploy-run refused the request: {exc}", flush=True)
        return lf.REFUSED
    places = Places.for_runner(Path(__file__).resolve().parents[3], state)
    return deploy.main(words, Paths(mirror=places.mirror), wait)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
