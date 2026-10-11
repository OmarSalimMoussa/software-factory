"""The watch over sessions, once a tick: bind and backstop, finish the benches whose grace has passed, retire each
session whose task moved past its part, resume or retire each working or waiting session whose process is gone, and
restart an orchestrator whose loop went silent.

A finished implementer or designer is retired at once, its process stopped and its slot reaped, though a background
session stays alive when idle: its place and slot are free for the next run. Only working and waiting sessions are
watched: a deprioritized one has no process by design, and a bench-pending or reviving one is in the warden's own
hands. A crashed session is resumed up to watch.resume_tries
times, then retired as crashed; the orchestrator is always resumed (the same session) and never retired by the watch.
While the owner's switch of Claude accounts runs, its daemon restart is no crash: the switch brings its sessions back.
"""

from __future__ import annotations

import logging
from datetime import datetime

from runtime.shared import store
from runtime.shared.models import (
    BENCH_PENDING,
    CRASHED,
    DEPRIORITIZED,
    FINISHED,
    WAITING,
    WITH_PARENT,
    WORKING,
    Session,
)
from runtime.shared.protocol import Refused
from runtime.slots import leases
from runtime.warden import backstop, inbox, launch, lifecycle, loop, permits, reflections, registry
from runtime.warden.backstop import Proc
from runtime.warden.context import Context
from runtime.warden.launch import LaunchError
from runtime.warden.permits import DESIGN, IMPL, ORCH

log = logging.getLogger(__name__)
WARDEN = lifecycle.WARDEN
RESTARTED = "The warden restarted you after your process ended: rerun your startup checks, then carry on."
SILENT = "The warden restarted you: your loop was silent for {minutes} min. Rerun your startup checks, then carry on."
IDLE = "idle"
# A session whose task has moved past its part is finished; a process that ends before that crashed.
DONE_AT = {
    IMPL: frozenset({"checked", "on-staging", "promoted", "closed"}),
    DESIGN: frozenset({"building", "qa", "qa-clean", "turn", "merged", "checked", "on-staging", "promoted", "closed"}),
}


def _age(ctx: Context, since: str) -> float:
    return ctx.clock.now() - datetime.fromisoformat(since).timestamp()


def watch_sessions(ctx: Context) -> None:
    """The loop calls this once a tick."""
    procs = backstop.processes()
    backstop.sweep(ctx, procs)
    finish_benches(ctx, procs)
    watch(ctx)
    watch_orchestrator(ctx)


def finish_benches(ctx: Context, procs: dict[str, Proc]) -> None:
    grace = ctx.settings().get_int("watch.bench_grace_seconds")
    due = [s for s in lifecycle.read(ctx).values() if s.state == BENCH_PENDING and _age(ctx, s.since) >= grace]
    for s in due:
        try:
            _settle(ctx, s, procs)
        except (LaunchError, Refused, store.StoreError) as exc:
            log.warning("bench of session %s not finished (%s); tried again next tick", s.id, type(exc).__name__)


def _settle(ctx: Context, s: Session, procs: dict[str, Proc]) -> None:
    """Stop the process, empty and give up its slot, then mark it deprioritized. Each step may run again: a slot
    that would not empty keeps its lease, and the next tick tries again."""
    if s.id in procs:
        ctx.launcher.stop_session(s.id)
    lease = leases.held_by(ctx.places, ctx.settings(), s.name)
    if lease is not None:
        failed = leases.empty(ctx, lease.slot, s.id)
        if failed:
            raise LaunchError(f"slot {lease.slot} did not empty: {failed}")
    with ctx.lock:
        sessions = lifecycle.read(ctx)
        current = sessions.get(s.id)
        if current is None or current.state != BENCH_PENDING:
            lifecycle.pause_progress(ctx, s.id, "already off the bench's list")
            return
        lifecycle.move(ctx, current, DEPRIORITIZED, WARDEN, current.note)
        # Its subagents end with it; on revive the parent starts them again.
        running = [c for c in registry.children(sessions, s.id) if c.state in (WORKING, WAITING)]
        lifecycle.retire(ctx, running, current, WITH_PARENT, WARDEN, "ended with its benched parent")
        registry.write(ctx.places, sessions)
    if s.task and permits.read_task(ctx, s.task).status in ("building", "designing"):
        permits.move_task(ctx, s.task, "deprioritized", "bench")
    lifecycle.pause_progress(ctx, s.id, "on the bench")
    ctx.post_event(inbox.BENCHED, {"session": s.id, "name": s.name, "task": s.task})


def _finished(ctx: Context, s: Session) -> bool:
    if s.role not in DONE_AT or not s.task:
        return False
    try:
        return permits.read_task(ctx, s.task).status in DONE_AT[s.role]
    except Refused:  # an unreadable task is never read as finished: the session is resumed instead
        return False


def watch(ctx: Context) -> None:
    if permits.switching(ctx) is not None:
        return
    settings = ctx.settings()
    tries, grace = settings.get_int("watch.resume_tries"), settings.get_int("watch.start_grace_seconds")
    watched = [s.id for s in lifecycle.read(ctx).values() if s.state in (WORKING, WAITING) and not s.parent]
    for sid in watched:
        retired: list[Session] = []
        with ctx.lock:
            sessions = lifecycle.read(ctx)
            s = sessions.get(sid)
            # Read again under the lock: a wake or a revive may have changed it since the list above.
            if s is None or s.state not in (WORKING, WAITING):
                continue
            if s.role != ORCH and _finished(ctx, s):
                why = "finished: its task moved past its part"
                retired = lifecycle.retire(ctx, registry.subtree(sessions, sid), s, FINISHED, WARDEN, why)
            elif sid in backstop.processes():
                continue
            elif sid.startswith(launch.PENDING):
                if _age(ctx, s.since) < grace:
                    continue
                why = f"its session never appeared within {grace} s of its start"
                retired = lifecycle.retire(ctx, registry.subtree(sessions, sid), s, CRASHED, WARDEN, why)
            elif s.role == ORCH or s.resume_tries < tries:
                s.resume_tries += 1  # never reset by a resume, so a session that keeps dying is retired
                registry.write(ctx.places, sessions)
                try:
                    launch.resume(ctx, s, RESTARTED)
                except LaunchError as exc:
                    log.warning("resume %d of crashed session %s failed (%s)", s.resume_tries, sid, type(exc).__name__)
                if s.role == ORCH:
                    reflections.incident(ctx, "The orchestrator's process ended; the warden resumed it.")
                continue
            else:
                why = f"its process ended and {tries} resumes did not keep it running"
                retired = lifecycle.retire(ctx, registry.subtree(sessions, sid), s, CRASHED, WARDEN, why)
            registry.write(ctx.places, sessions)
        lifecycle.after_retire(ctx, retired)
        if retired[0].retired == CRASHED:
            ctx.post_event(inbox.CRASHED, {"session": sid, "name": retired[0].name, "task": retired[0].task})


def watch_orchestrator(ctx: Context) -> None:
    """Restart the orchestrator when no loop pass came for its silent setting (longer while it is busy)."""
    settings = ctx.settings()
    with ctx.lock:
        sessions = lifecycle.read(ctx)
        orchestrator = next((s for s in sessions.values() if s.role == ORCH and s.state in (WORKING, WAITING)), None)
        running = backstop.processes()
        if orchestrator is None or orchestrator.id not in running:  # not started, or a crash the watch above resumes
            return
        busy = running[orchestrator.id].status != IDLE
        limit = settings.get_int(f"watch.orchestrator_{'busy_' if busy else ''}silent_minutes") * 60
        heard = max(loop.last_tick(ctx.places) or 0.0, datetime.fromisoformat(orchestrator.since).timestamp())
        silent = ctx.clock.now() - heard
        if silent < limit:
            return
        try:
            lifecycle.restart(ctx, orchestrator, SILENT.format(minutes=int(silent // 60)))
            reflections.incident(ctx, f"The orchestrator's loop was silent for {int(silent // 60)} min; the warden "
                                      "restarted it.")  # fmt: skip
        except LaunchError as exc:
            log.warning("restart of silent orchestrator %s failed (%s)", orchestrator.id, type(exc).__name__)
            return
        # Its silence counts again from now, so the next tick does not restart it once more.
        lifecycle.move(ctx, orchestrator, orchestrator.state, WARDEN, f"restarted after {int(silent // 60)} min silent")
        registry.write(ctx.places, sessions)
