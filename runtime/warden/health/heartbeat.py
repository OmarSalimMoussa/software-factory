"""Healthchecks.io pings: the warden's own check on each probe pass, the orchestrator's on each loop pass it ends.

The installer keeps the ping addresses in secrets/healthchecks.json. Installed without --healthchecks, there is no
file and nothing is pinged. A missed ping is what the check exists to catch, so a failed one is only logged.
"""

from __future__ import annotations

import json
import logging

from runtime.shared import network
from runtime.warden.context import Context

log = logging.getLogger(__name__)
WARDEN, ORCHESTRATOR = "warden", "orchestrator"


def ping(ctx: Context, check: str) -> None:
    path = ctx.places.state / "secrets" / "healthchecks.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    # telemetry: expected — the factory was installed without --healthchecks
    except FileNotFoundError:
        return
    url = record["pings"].get(check)
    if not url:
        return
    try:
        network.call(url, ctx.settings().get_int("watch.http_timeout_seconds"), follow=True).checked("Healthchecks.io")
    except OSError as exc:
        log.warning("warden: the %s healthcheck ping failed (%s)", check, type(exc).__name__)
