"""The line for a browser place. The guard refuses a browser tester past the machine's browser places, or on red vitals
(`browsers-at-once`), and a session cannot see another session's testers hand back. So the warden keeps each refused
session in a line, in the order the guard refused them, and each pass tells as many as there are free places, the
longest waiting first, while the vitals are not red. A place told of is kept for its session qa.place_turn_seconds;
a told session refused again, because another took the place, goes back to the head of the line. A session leaves
the line once it starts a browser tester or retires.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from runtime.shared import lanes
from runtime.shared.models import RETIRED
from runtime.shared.protocol import Refused
from runtime.warden.context import Context
from runtime.warden.sessions import lifecycle, permits, registry
from runtime.warden.sessions.launch import LaunchError

log = logging.getLogger(__name__)

FREE = "A browser place is free on this machine: start the browser tester the guard held back, if you still need it."


def refused(ctx: Context, line: dict[str, Any]) -> None:
    """A guard refusal: a session refused a browser place joins the line once, at the head if it had been told."""
    sid = line.get("session")
    if line.get("rule") != lanes.PLACES or not isinstance(sid, str) or not sid:
        return
    with ctx.lock:
        waiting, told = lanes.line(ctx.places)
        if sid not in waiting:
            waiting = [sid, *waiting] if told.pop(sid, None) is not None else [*waiting, sid]
            lanes.write_line(ctx.places, waiting, told)


def leave(ctx: Context, sid: str) -> None:
    with ctx.lock:
        waiting, told = lanes.line(ctx.places)
        if sid in waiting or sid in told:
            told.pop(sid, None)
            lanes.write_line(ctx.places, [s for s in waiting if s != sid], told)


def run_pass(ctx: Context) -> None:
    with ctx.lock:
        waiting, told = lanes.line(ctx.places)
        if not waiting and not told:
            return
        sessions, settings, now = registry.read(ctx.places), ctx.settings(), ctx.clock.now()
        live = {sid for sid, s in sessions.items() if s.state != RETIRED}
        turn = settings.get_int("qa.place_turn_seconds")
        told = {sid: at for sid, at in told.items() if sid in live and now - at < turn}
        waiting = [sid for sid in waiting if sid in live]
        held = lanes.holders(sessions.values(), settings, datetime.fromtimestamp(now, UTC))
        red = permits.vitals_level(ctx)[0] == "red"
        free = 0 if red else max(settings.get_int("caps.browsers_at_once") - len(held) - len(told), 0)
        tell, waiting = waiting[:free], waiting[free:]
        lanes.write_line(ctx.places, waiting, {**told, **dict.fromkeys(tell, now)})
    for sid in tell:
        try:
            lifecycle.deliver(ctx, sid, FREE, lifecycle.WARDEN)
        except (LaunchError, Refused) as exc:
            log.warning("warden: telling %s a browser place is free failed (%s)", sid, type(exc).__name__)
