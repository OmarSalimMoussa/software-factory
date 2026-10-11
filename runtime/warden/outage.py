"""A clear prod outage, and the one fix it lets start without the owner (plan R18, the routing spec).

outage.json holds the outage while it lasts, through its stages: down (the last watch.outage_after prod probes all
counted as failures), fixing (the one bypass run started, at rung 1 or 2), on-staging (staging runs the fix and the
owner decides the push), stopped (the owner stopped the fix). The outage ends, and the file goes, when prod's probe
is green again. bypass_used is set once a run started, so no second run ever skips the owner for the same outage.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from runtime.shared import models, store
from runtime.shared.models import Outage, Probe
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused, ok
from runtime.warden import deploys, inbox, probes, reflections, registry
from runtime.warden.context import Context

DOWN, FIXING, ON_STAGING, STOPPED = "down", "fixing", "on-staging", "stopped"
FIX_ON_STAGING = ("on-staging", "promoted")  # task statuses that mean staging's build includes the fix


def path(places: Places) -> Path:
    return places.state / "outage.json"


def read(places: Places) -> Outage | None:
    raw = store.read_json_or(path(places), None)
    return None if raw is None else models.load(Outage, raw)


def _write(places: Places, outage: Outage) -> None:
    store.write_json(path(places), models.dump(outage))


def observe(ctx: Context, probe: Probe) -> None:
    """Feed one prod probe: a green one ends the outage; enough counted failures in a row start one."""
    with ctx.lock:
        current = read(ctx.places)
        if probe.ok:
            if current is not None:
                path(ctx.places).unlink()
            return
        if current is not None or not probe.counts:
            return
        if deploys.in_flight(ctx.places, probes.PROD, ctx.clock.now()):
            return  # the owner's push restarts prod; a failure until it answers on the new build is the restart
        needed = ctx.settings().get_int("watch.outage_after")
        streak = probes.recent(ctx.places, probes.PROD, needed)
        if len(streak) < needed or not all(p.counts for p in streak):
            return
        outage = Outage(since=streak[0].at, stage=DOWN)
        _write(ctx.places, outage)
    reflections.incident(ctx, f"Prod went down at {outage.since}: {needed} health checks in a row failed.")
    from runtime.warden import loop  # the loop feeds this module its probes

    body = (f"Prod's health check failed {needed} probes in a row since {outage.since}: {probe.error}. Prod cannot be "
            "fixed on prod: the fix ships to staging for the owner to push, or the owner pushes prod back. One run at "
            "rung 1 or 2 may start without the owner's approval.")  # fmt: skip
    req = loop.file_red(ctx, "prod", body)
    ctx.post_event(inbox.PROD_RED, {"since": outage.since, "probes": [models.dump(p) for p in streak], "request": req})


def bypass_allowed(ctx: Context) -> bool:
    """Whether a run may start without the owner's approval: prod is down and its one fix has not started."""
    outage = read(ctx.places)
    return outage is not None and outage.stage == DOWN and not outage.bypass_used


def mark_bypass(ctx: Context, task: str, session: str) -> None:
    """Record the one bypass run; refuses a second. Hold ctx.lock from bypass_allowed through the start to here."""
    with ctx.lock:
        outage = read(ctx.places)
        if outage is None or outage.stage != DOWN or outage.bypass_used:
            raise Refused(
                "outage-bypass", "the outage fix already ran or prod is up: this run needs the owner's approval"
            )
        outage.stage, outage.task, outage.session, outage.bypass_used = FIXING, task, session, True
        _write(ctx.places, outage)


def fix_status(ctx: Context, task_status: str) -> None:
    """The fix task's status, read each tick while fixing: on staging, the push waits for the owner."""
    with ctx.lock:
        outage = read(ctx.places)
        if outage is not None and outage.stage == FIXING and task_status in FIX_ON_STAGING:
            outage.stage = ON_STAGING
            _write(ctx.places, outage)


def owner_stop_fix(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """The owner's Stop on the outage band: the fix session retires and no bypass run follows."""
    # Imported here: lifecycle's permits import this module.
    from runtime.warden.lifecycle import retire_session

    with ctx.lock:
        outage = read(ctx.places)
        if outage is None:
            raise Refused("outage", "prod is not down, so there is no fix to stop")
        if outage.stage == STOPPED:
            raise Refused("outage", "the fix is already stopped")
        if outage.stage == ON_STAGING:
            raise Refused("outage", "the fix is already on staging: push it to prod, or push prod back")
        fixer = registry.read(ctx.places).get(outage.session)
        if fixer is not None and fixer.state != models.RETIRED:
            retire_session(ctx, outage.session, models.STOPPED, "owner")
        outage.stage, outage.bypass_used = STOPPED, True
        _write(ctx.places, outage)
    ctx.post_event(
        "stopped", {"session": outage.session, "task": outage.task, "why": "the owner stopped the outage fix"}
    )
    return ok(message="The fix is stopped. Prod stays down until you push a build or push prod back.")
