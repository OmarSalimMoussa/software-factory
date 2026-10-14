"""The watch over sessions, once a tick: bind and backstop, finish the benches whose grace has passed (in a thread of
their own), retire each session whose task moved past its part, resume or retire each working or waiting session
whose process is gone, and restart an orchestrator whose loop went silent.

A finished implementer or designer is retired at once, its process stopped and its slot reaped, though a background
session stays alive when idle: its place and slot are free for the next run. Only working and waiting sessions are
watched: a deprioritized one has no process by design, and a bench-pending or reviving one is in the warden's own
hands. A crashed session is resumed up to watch.resume_tries times within the crash window, each resume after a
longer wait, then retired as crashed; the orchestrator is always resumed (the same session) and never retired by the
watch. Nothing resumes until the agent's Claude daemon is up and settled, and sessions that ended with the daemon (a
reboot, a daemon restart) come back without counting as crashed.
While the owner's switch of Claude accounts runs, its daemon restart is no crash: the switch brings its sessions back.
While the vitals find Claude sessions with no record the warden reads, no session counts as crashed.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from runtime.queue import formats
from runtime.shared import store
from runtime.shared.models import (
    BENCH_PENDING,
    CRASHED,
    DEPRIORITIZED,
    FINISHED,
    ON_STAGING,
    PENDING,
    QA_CLEAN,
    WAITING,
    WORKING,
    Session,
)
from runtime.shared.protocol import Refused
from runtime.slots import leases
from runtime.warden import imports, inbox, loop
from runtime.warden.context import Context
from runtime.warden.health import vitals
from runtime.warden.owner import reflections
from runtime.warden.sessions import backstop, lifecycle, permits, registry, respawn, revive
from runtime.warden.sessions.backstop import Proc
from runtime.warden.sessions.launch import LaunchError
from runtime.warden.sessions.permits import DESIGN, IMPL, ORCH

log = logging.getLogger(__name__)
WARDEN = lifecycle.WARDEN
RESTARTED = "The warden restarted you after your process ended: rerun your startup checks, then carry on."
SILENT = "The warden restarted you: your loop was silent for {minutes} min. Rerun your startup checks, then carry on."
OWED = " Events you have not acted on yet: {kinds}. `factory inbox` lists them."
IDLE = "idle"
# A session whose task has moved past its part is finished; a process that ends before that crashed.
DONE_AT = {
    IMPL: frozenset({"checked", ON_STAGING, "promoted", "closed"}),
    DESIGN: frozenset({"building", "qa", QA_CLEAN, "turn", "merged", "checked", ON_STAGING, "promoted", "closed"}),
}


class SessionsWatch:
    """The sessions pass, once a tick. Benches settle in a thread of their own, as reaps do: emptying a slot runs the
    product's scripts for up to slots.verb_minutes, and probes, outages and check-ins must not wait on it. ``start``
    runs the settling (a test passes one that runs it at once)."""

    def __init__(self, start: imports.Start) -> None:
        self._start = start
        self._benches: threading.Thread | None = None

    def run_pass(self, ctx: Context) -> None:
        procs = backstop.processes()
        backstop.sweep(ctx, procs)
        if self._benches is None or not self._benches.is_alive():
            # In its own thread: a failure is reported there, once while it lasts, as a pass's is.
            self._benches = self._start(lambda: loop.run_once(ctx, "the benches' settling",
                                                              lambda: finish_benches(ctx, procs)))  # fmt: skip
        watch(ctx)
        watch_orchestrator(ctx)


def finish_benches(ctx: Context, procs: dict[str, Proc]) -> None:
    grace = ctx.settings().get_int("watch.bench_grace_seconds")
    due = [s for s in lifecycle.read(ctx).values() if s.state == BENCH_PENDING and ctx.clock.age(s.since) >= grace]
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
            revive.pause_progress(ctx, s.id, "already off the bench's list")
            return
        lifecycle.move(ctx, current, DEPRIORITIZED, WARDEN, current.note)
        lifecycle.end_children(ctx, sessions, current, "ended with its benched parent")  # its revive starts them
        registry.write(ctx.places, sessions)
    if s.task and permits.read_task(ctx, s.task).status in ("building", "designing"):
        permits.move_task(ctx, s.task, "deprioritized", "bench")
    revive.pause_progress(ctx, s.id, "on the bench")
    ctx.post_event(inbox.BENCHED, {"session": s.id, "name": s.name, "task": s.task})


def _finished(ctx: Context, s: Session) -> bool:
    if s.role not in DONE_AT or not s.task:
        return False
    try:
        return permits.read_task(ctx, s.task).status in DONE_AT[s.role]
    # telemetry: expected — an unreadable task is never read as finished: the session is resumed instead
    except Refused:
        return False


def _daemon_path(ctx: Context) -> Path:
    return ctx.places.state / "daemon-seen.json"


def _daemon_seen(ctx: Context) -> float | None:
    """When the daemon the last sessions pass saw had started; None before the first pass that saw one."""
    found = store.read_json_or(_daemon_path(ctx), None)
    return (
        float(found["started"]) if isinstance(found, dict) and isinstance(found.get("started"), int | float) else None
    )


@dataclass(frozen=True)
class _Pass:
    """What one watch pass knows for every session it looks at."""

    now: float
    tries: int
    grace: int
    window: int
    backoff: int
    daemon: float | None
    seen: float | None
    hosting: bool
    restarted: bool


# What becomes of a watched session: those retired with it, and the session to resume (and whether its resume counts).
_Outcome = tuple[list[Session], tuple[Session, bool] | None]


def _pass(ctx: Context) -> _Pass:
    settings, now = ctx.settings(), ctx.clock.now()
    daemon = backstop.daemon_started()
    # No session can come back until the daemon that hosts it is up and has had a moment to be ready (a boot starts
    # the warden first). Sessions found gone just after the daemon started again ended with it: no crash of theirs.
    hosting = daemon is not None and now - daemon >= settings.get_int("watch.daemon_settle_seconds")
    seen = _daemon_seen(ctx)
    return _Pass(
        now=now,
        tries=settings.get_int("watch.resume_tries"),
        grace=settings.get_int("watch.start_grace_seconds"),
        window=settings.get_int("watch.crash_window_minutes") * 60,
        backoff=settings.get_int("watch.resume_backoff_seconds"),
        daemon=daemon,
        seen=seen,
        hosting=hosting,
        restarted=hosting and seen is not None and seen != daemon,
    )


def watch(ctx: Context) -> None:
    # Sessions the warden cannot see would all look crashed: it resumes and retires none until it sees them again.
    if permits.switching(ctx) is not None or vitals.blind(ctx.places):
        return
    facts = _pass(ctx)
    watched = [s.id for s in lifecycle.read(ctx).values() if s.state in (WORKING, WAITING) and not s.parent]
    for sid in watched:
        with ctx.lock:
            sessions = lifecycle.read(ctx)
            s = sessions.get(sid)
            # Read again under the lock: a wake or a revive may have changed it since the list above.
            if s is None or s.state not in (WORKING, WAITING) or lifecycle.launching(sid):
                continue
            judged = _judge(ctx, facts, sessions, sid, s)
            if judged is None:
                continue
            retired, relaunch = judged
            if relaunch is not None:
                lifecycle.end_children(ctx, sessions, s, "its parent's process ended")
            registry.write(ctx.places, sessions)
        _act(ctx, sid, retired, relaunch)
    if facts.hosting and facts.daemon != facts.seen:
        store.write_json(_daemon_path(ctx), {"started": facts.daemon})


def _judge(ctx: Context, facts: _Pass, sessions: dict[str, Session], sid: str, s: Session) -> _Outcome | None:
    """What becomes of a watched session, under ctx.lock; None leaves it, and its record, as they are."""
    if s.role != ORCH and _finished(ctx, s):
        why = "finished: its task moved past its part"
        return lifecycle.retire(ctx, registry.subtree(sessions, sid), s, FINISHED, WARDEN, why), None
    if sid in backstop.processes() or not facts.hosting:
        return None
    if sid.startswith(PENDING):
        if ctx.clock.age(s.since) < facts.grace:
            return None
        why = f"its session never appeared within {facts.grace} s of its start"
        return lifecycle.retire(ctx, registry.subtree(sessions, sid), s, CRASHED, WARDEN, why), None
    if facts.restarted:
        return [], ((s, False) if lifecycle.claim_launch(ctx, sid) else None)
    return _crashed(ctx, facts, sessions, sid, s)


def _crashed(ctx: Context, facts: _Pass, sessions: dict[str, Session], sid: str, s: Session) -> _Outcome | None:
    """A session whose process ended is resumed, waiting longer each time a resume did not hold, until its tries run
    out within the crash window; then it is retired. The orchestrator is never retired."""
    last = formats.epoch(s.crashed_at) if s.crashed_at else None
    if last is not None and facts.now - last > facts.window:
        s.resume_tries = 0  # its last crash was long ago: this one counts afresh
    if (
        last is not None
        and s.resume_tries
        and facts.now < last + min(facts.backoff * 2 ** (s.resume_tries - 1), facts.window)
    ):
        return None  # backing off: the last resume did not hold
    tries, window = facts.tries, facts.window
    if s.role == ORCH or s.resume_tries < tries:
        s.resume_tries += 1  # a resume never resets it: one that keeps dying is retired
        s.crashed_at = ctx.clock.iso()
        relaunch = (s, True) if lifecycle.claim_launch(ctx, sid) else None
        if s.role == ORCH and s.resume_tries == tries:
            reflections.incident(ctx, f"The orchestrator's process ended {tries} times within "
                                      f"{window // 60} min; the warden keeps resuming it, waiting longer "
                                      "each time. Check its Claude account's sign-in.")  # fmt: skip
        return [], relaunch
    why = f"its process ended and {tries} resumes within {window // 60} min did not keep it running"
    return lifecycle.retire(ctx, registry.subtree(sessions, sid), s, CRASHED, WARDEN, why), None


def _act(ctx: Context, sid: str, retired: list[Session], relaunch: tuple[Session, bool] | None) -> None:
    """Outside the lock: resume the session claimed for it, or carry out the retirements."""
    if relaunch is not None:
        _resume(ctx, *relaunch)
        return
    if not retired:
        return
    lifecycle.after_retire(ctx, retired)
    if retired[0].retired == CRASHED:
        ctx.post_event(inbox.CRASHED, {"session": sid, "name": retired[0].name, "task": retired[0].task})


def _resume(ctx: Context, s: Session, counted: bool) -> None:
    """Resume ``s``, claimed for it under ctx.lock, outside the lock."""
    try:
        respawn.resume(ctx, s, RESTARTED + (_owed(ctx) if s.role == ORCH else ""))
    except LaunchError as exc:
        log.warning("resume of session %s after its process ended failed (%s)", s.id, type(exc).__name__)
    finally:
        lifecycle.release_launch(ctx, s.id)
    if s.role == ORCH and counted:
        reflections.incident(ctx, "The orchestrator's process ended; the warden resumed it.")


def _owed(ctx: Context) -> str:
    """The orchestrator's restart message names the events it has not acted on, so none is lost with its process."""
    kinds = [e["kind"] for e in inbox.unacknowledged(ctx.places)]
    return OWED.format(kinds=", ".join(dict.fromkeys(kinds))) if kinds else ""


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
        lifecycle.restart(ctx, orchestrator, SILENT.format(minutes=int(silent // 60)) + _owed(ctx))
    except LaunchError as exc:
        log.warning("restart of silent orchestrator %s failed (%s)", orchestrator.id, type(exc).__name__)
        return
    reflections.incident(ctx, f"The orchestrator's loop was silent for {int(silent // 60)} min; the warden "
                              "restarted it.")  # fmt: skip
    with ctx.lock:
        sessions = lifecycle.read(ctx)
        current = sessions.get(orchestrator.id)
        if current is None or current.state not in (WORKING, WAITING):
            return
        # Its silence counts again from now, so the next tick does not restart it once more.
        lifecycle.move(ctx, current, current.state, WARDEN, f"restarted after {int(silent // 60)} min silent")
        registry.write(ctx.places, sessions)
