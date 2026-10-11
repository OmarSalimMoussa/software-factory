"""The warden's scheduling pass, once a tick: what should run, set against what runs.

Each role's candidates, in the one order: its deprioritized sessions, the owner's approved runs, and the designed
builds waiting for their implementer. The first one that fits is brought back or started when the vitals and the
role's cap allow. When the cap is full, the lowest-ranked seated session that candidate outranks is benched (a
waiting one before a working one, never one in its merge turn), and the role waits for that bench to settle. A run
starts with the brief the orchestrator wrote when it proposed it; nothing waits on an event, and the orchestrator
starts nothing. Why each candidate still waits is kept for `factory status` and the dashboard.
"""

from __future__ import annotations

import logging
from pathlib import Path

from runtime.queue import proposals
from runtime.shared import order, store
from runtime.shared.models import BENCH_PENDING, DEPRIORITIZED, RETIRED, REVIVING, Session, Task
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused
from runtime.warden.context import Context
from runtime.warden.sessions import lifecycle, permits, registry, revive
from runtime.warden.sessions.permits import DESIGN, IMPL

log = logging.getLogger(__name__)
WARDEN = lifecycle.WARDEN


def waits_path(places: Places) -> Path:
    return places.state / "start-waits.json"


def waits(places: Places) -> dict[str, str]:
    """Why each approved run, designed build and deprioritized session has not started yet, by task."""
    raw = store.read_json_or(waits_path(places), {})
    if not isinstance(raw, dict):
        raise store.StoreError(f"{waits_path(places)}: not an object")
    return raw


def run_pass(ctx: Context) -> None:
    # Runs belong to a working factory: none starts while it is paused, while the accounts switch, or once the owner
    # stopped it (the orchestrator retired, and the installer may be waiting for every session to end).
    if lifecycle.paused_path(ctx).exists() or permits.switching(ctx) is not None:
        return
    if not permits.at_work(registry.read(ctx.places).values()):
        return
    waiting: dict[str, str] = {}
    for role in (IMPL, DESIGN):
        _role(ctx, role, waiting)
    store.write_json(waits_path(ctx.places), waiting)


def _candidates(ctx: Context, sessions: dict[str, Session], role: str) -> list[tuple[Task, Session | None]]:
    approved = permits.approved_order(ctx)
    live = {s.task for s in sessions.values() if s.role == role and s.state != RETIRED and not s.parent}
    found: list[tuple[Task, Session | None]] = []
    for s in sessions.values():
        if s.role == role and s.state == DEPRIORITIZED and not s.parent and s.task:
            found.append((permits.read_task(ctx, s.task), s))
    ids = list(approved) + (proposals.designed_builds(ctx.places) if role == IMPL else [])
    for task_id in dict.fromkeys(ids):
        if task_id in live:
            continue
        task = permits.read_task(ctx, task_id)
        if task.status in ("approved", "building") and permits.needed_role(ctx, task_id) == role:
            found.append((task, None))
    ranking = permits.ranking(ctx)
    return sorted(found, key=lambda pair: order.rank(pair[0], ranking))


def _role(ctx: Context, role: str, waiting: dict[str, str]) -> None:
    sessions = lifecycle.read(ctx)
    if any(s.role == role and s.state in (BENCH_PENDING, REVIVING) and not s.parent for s in sessions.values()):
        waiting.update({t.id: "waits while another run settles its place" for t, _ in _candidates(ctx, sessions, role)})
        return
    for task, benched in _candidates(ctx, sessions, role):
        try:
            # The cheap checks first, so a candidate that waits for a place costs no git call.
            permits.check_vitals(ctx, task.severity == permits.SEVERE)
            permits.check_cap(ctx.settings(), sessions.values(), role)
            if benched is not None:
                revive.revive_session(ctx, benched.id, WARDEN)
            else:
                lifecycle.start_run(ctx, role, task, "", outage_fix=False)
        # telemetry: surfaced — why it is `waiting` is kept for `factory status` and the dashboard
        except Refused as refused:
            waiting[task.id] = refused.reason
            if refused.rule not in ("cap", "revive-first"):
                continue  # this candidate cannot run as it is; the next one still may
            if refused.rule == "cap" and _make_room(ctx, lifecycle.read(ctx), role, task):
                waiting[task.id] = "a lower-ranked run is pausing to make room"
            return  # nothing below a candidate that waits for a place may take it


def _make_room(ctx: Context, sessions: dict[str, Session], role: str, task: Task) -> bool:
    """Bench the lowest-ranked seated session ``task`` outranks: a waiting one before a working one."""
    choices = permits.benchable(ctx, sessions, role, task.id)
    if not choices:
        return False
    with ctx.lock:
        sessions = lifecycle.read(ctx)
        victim = sessions[choices[0].id]
        lifecycle.begin_bench(ctx, victim, WARDEN, f"benched for {task.id}", task.id)
        registry.write(ctx.places, sessions)
    log.info("warden: benched %s to make room for %s", victim.name, task.id)
    return True
