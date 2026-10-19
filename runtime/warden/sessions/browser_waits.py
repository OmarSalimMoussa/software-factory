"""The line for a browser place. The guard refuses a browser tester past the machine's browser places, on red vitals,
or while a tester holds a desktop (`browsers-at-once`), and a session cannot see another session's testers hand back.
So the warden keeps each refused session in a line, in the order the guard refused them, and each pass tells as many
as there are free places, the longest waiting first, while the vitals are not red and no tester at work holds a
desktop, as a desktop walk runs alone. The orchestrator joins at the head: its merge check is what every merge waits
on, and the guard holds back other testers while it waits. A place told of is kept for its session
qa.place_turn_seconds, which the guard counts as taken for every other session; a told session refused again goes
back to the head of the line. A session leaves the line once it starts a browser tester or retires.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from runtime.guard.facts import read_desktops
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
            first = told.pop(sid, None) is not None or _orchestrator(ctx, sid)
            waiting = [sid, *waiting] if first else [*waiting, sid]
            lanes.write_line(ctx.places, waiting, told)


def _orchestrator(ctx: Context, sid: str) -> bool:
    session = registry.read(ctx.places).get(sid)
    return session is not None and session.role == permits.ORCH


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
        when = datetime.fromtimestamp(now, UTC)
        held = lanes.holders(sessions.values(), settings, when)
        alone = lanes.desktop_walks(sessions.values(), read_desktops(ctx.places), settings, when)
        red = permits.vitals_level(ctx)[0] == "red"
        free = 0 if red or alone else max(settings.get_int("caps.browsers_at_once") - len(held) - len(told), 0)
        tell, waiting = waiting[:free], waiting[free:]
        lanes.write_line(ctx.places, waiting, {**told, **dict.fromkeys(tell, now)})
    for sid in tell:
        try:
            lifecycle.deliver(ctx, sid, FREE, lifecycle.WARDEN)
        except (LaunchError, Refused) as exc:
            log.warning("warden: telling %s a browser place is free failed (%s)", sid, type(exc).__name__)
