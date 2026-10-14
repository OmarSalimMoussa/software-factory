"""Slot containers that stopped when nothing asked them to: each one that ran out of memory, or died with a failing
exit and no kill before it. The warden reports each to its own GlitchTip, once.

Docker belongs to the stack user, so the stack unit reads its events (leases.EVENTS) for the span since the last
read, every ``watch.container_events_minutes``. Every stop the factory makes (a rebuild, down, empty, a sweep) kills
the container first, so a death after a kill was asked for. The first read starts the span: what came before it is
not judged.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from runtime.shared import store
from runtime.shared.paths import Places
from runtime.slots import leases
from runtime.warden.context import Context

log = logging.getLogger("factory.warden")

_NO_SLOT = 0
_SESSION = "container-events"


class EventsError(Exception):
    """The stack unit could not read Docker's events."""


def _cursor(places: Places) -> Path:
    return places.state / "container-events.json"


def unasked(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each container's first out-of-memory, or failing death with no kill before it, in the order they came."""
    killed: set[str] = set()
    found: dict[str, dict[str, Any]] = {}
    for event in sorted(events, key=lambda e: int(e["at"])):
        if event["action"] == "kill":
            killed.add(event["id"])
        elif event["action"] == "oom" or (event["exit"] not in ("", "0") and event["id"] not in killed):
            found.setdefault(event["id"], event)
    return list(found.values())


def run_pass(ctx: Context) -> int:
    """Read the events since the last read, when it is due, and report each unasked stop; how many were reported."""
    now, stops = int(ctx.clock.now()), []
    since = store.read_json_or(_cursor(ctx.places), {}).get("until")
    if since is not None and now - since < ctx.settings().get_int("watch.container_events_minutes") * 60:
        return 0
    if since is not None:
        done = ctx.launcher.run_stack(_NO_SLOT, [leases.EVENTS, str(since), str(now)], _SESSION)
        if done.exit != 0:
            raise EventsError(f"the stack unit could not read Docker's events (exit {done.exit})")
        lines = Path(done.log).read_text(encoding="utf-8").splitlines()
        stops = unasked([json.loads(line) for line in lines if line.startswith("{")])
        for e in stops:
            what = "ran out of memory" if e["action"] == "oom" else f"died with exit {e['exit']}"
            log.error("warden: slot %s's container %s %s, and the factory did not stop it", e["slot"], e["name"], what)
    store.write_json(_cursor(ctx.places), {"until": now})
    return len(stops)
