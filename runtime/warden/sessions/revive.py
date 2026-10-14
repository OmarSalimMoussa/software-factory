"""Bringing benched sessions back: a deprioritized session the orchestrator asks to revive or the scheduling pass
picks, and the owner's pause and resume of the whole factory, which benches every running session and brings each
back in the order it benched them.
"""

from __future__ import annotations

import logging
from typing import Any

from runtime.shared import models, operations, store
from runtime.shared.models import RETIRED, WAITING, WORKING
from runtime.shared.protocol import Refused, ok
from runtime.warden import inbox
from runtime.warden.context import Context
from runtime.warden.owner import ops
from runtime.warden.sessions import older_skills, permits, registry, respawn
from runtime.warden.sessions.launch import LaunchError
from runtime.warden.sessions.lifecycle import (
    OWNER,
    WARDEN,
    begin_bench,
    deliver,
    held_word,
    move,
    paused_path,
    read,
    took_held,
)
from runtime.warden.sessions.permits import CAP, DESIGN, IMPL, SEVERE

log = logging.getLogger(__name__)
PAUSED = {"message": "The factory is paused; every session keeps its context.", "refresh": "all"}
REVIVED = "The warden brought you back: carry on with your task from your branch."


def bring_back(ctx: Context, sid: str, by: str, note: str = "") -> str:
    """Resume a session already marked reviving, claiming a slot for an implementer; why it failed, or nothing.

    ``note`` follows the warden's own words in the message it comes back to: the owner's message, when one brought it.
    """
    s = read(ctx)[sid]
    slot = None
    try:
        if s.role == IMPL:
            slot = permits.claim_slot(ctx, s)
        rebuild = f" Slot {slot} is yours again: rebuild it with factory slot up." if slot is not None else ""
        older = older_skills.holds(ctx.places, s.id)
        held = held_word(ctx, s.task)
        said = "\n\n".join(part for part in (older_skills.RELOAD if older else "", note, held) if part)
        respawn.resume(ctx, s, REVIVED + rebuild + (f"\n\n{said}" if said else ""))
    # telemetry: surfaced — the session stays deprioritized; `move` records why, and the caller is told
    except (LaunchError, Refused) as exc:
        if slot is not None:
            permits.free_slot(ctx, slot)
        with ctx.lock:
            sessions = read(ctx)
            move(ctx, sessions[sid], models.DEPRIORITIZED, by, f"revive failed: {exc}")
            registry.write(ctx.places, sessions)
        return str(exc)
    with ctx.lock:
        sessions = read(ctx)
        s = sessions[sid]
        s.resume_tries, s.benched_for = 0, ""
        move(ctx, s, WORKING, by, "revived")
        registry.write(ctx.places, sessions)
        if older:
            older_skills.told(ctx.places, s.id)
        # A word held while it came back (a revert said as it revived) was not in its message: it follows now.
        late = took_held(ctx, s.task, held)
    if late:
        hand_held(ctx, s.id, s.task, late)
    if s.task and permits.read_task(ctx, s.task).status == "deprioritized":
        permits.move_task(ctx, s.task, "designing" if s.role == DESIGN else "building", "revive")
    return ""


def hand_held(ctx: Context, sid: str, task_id: str, word: str) -> None:
    """Hand a working session a word held for its task's run; one that does not arrive stays held for the run's
    next start or revive."""
    try:
        deliver(ctx, sid, word, WARDEN)
    except (LaunchError, Refused) as exc:
        log.warning("warden: a word held for %s did not reach session %s (%s)", task_id, sid, type(exc).__name__)
        return
    with ctx.lock:
        took_held(ctx, task_id, word)


def revive_session(ctx: Context, sid: str, by: str) -> str:
    """Bring a deprioritized session back, as the orchestrator asked or the warden's scheduling pass decided; its
    name. Raises Refused naming the rule it breaks."""
    with ctx.lock:
        sessions = read(ctx)
        s = sessions[sid]
        if s.state == RETIRED:
            raise Refused("retired", f"{s.name} is retired, and a retired session can never be revived")
        if s.state != models.DEPRIORITIZED:
            raise Refused("state", f"{s.name} is {s.state}; only a deprioritized session is revived")
        task = permits.read_task(ctx, s.task) if s.task else None
        permits.check_vitals(ctx, task is None or task.severity == SEVERE)
        permits.check_daemon(ctx)
        if s.role in CAP and task is not None:
            permits.check_cap(ctx.settings(), sessions.values(), s.role)
            permits.check_revive_order(ctx, sessions, s.role, task, skip=s.id)
        move(ctx, s, models.REVIVING, by, "reviving")
        registry.write(ctx.places, sessions)
    failed = bring_back(ctx, s.id, by)
    if failed:
        raise Refused("revive", f"{s.name} stays deprioritized: {failed}")
    return s.name


def owner_pause(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """Bench every running session, the orchestrator included; nothing is lost, and resume brings them back in order."""
    with ctx.lock:
        if paused_path(ctx).exists():
            raise Refused("paused", "the factory is already paused")
        sessions = read(ctx)
        if any(s.state == models.REVIVING for s in sessions.values()):
            raise Refused("reviving", "a session is coming back right now; pause again in a moment")
        ranked = permits.in_order(ctx, [s for s in sessions.values() if not s.parent and s.state in (WORKING, WAITING)])
        for s in ranked:
            begin_bench(ctx, s, OWNER, "paused with the factory", models.PAUSED)
        registry.write(ctx.places, sessions)
        # One step per session; the crash watch completes each as it settles that bench (pause_progress).
        op_id = ops.start(
            ctx, operations.PAUSE, ["Ask every session to commit and stop", *(f"Bench {s.name}" for s in ranked)]
        )
        for index in range(1, len(ranked) + 1):
            ops.step(ctx, op_id, index, ops.RUNNING)
        ops.complete(ctx, op_id, 0, f"{len(ranked)} told", PAUSED)
        record = {"at": ctx.clock.iso(), "sessions": [s.id for s in ranked], "op": op_id}
        store.write_json(paused_path(ctx), record, mode=0o600)
    ctx.post_event(inbox.PAUSED, {"sessions": [s.id for s in ranked]})
    return ok(op=op_id, benched=[s.name for s in ranked],
              grace_seconds=ctx.settings().get_int("watch.bench_grace_seconds"))  # fmt: skip


def pause_progress(ctx: Context, sid: str, detail: str) -> None:
    """A session the pause benched has settled: its step is done, and the last one finishes the pause."""
    record = store.read_json_or(paused_path(ctx), None)
    if record is None or sid not in record["sessions"]:
        return
    ops.complete(ctx, record["op"], 1 + record["sessions"].index(sid), detail, PAUSED)


def owner_resume(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """Bring back every session the pause benched, in the order it benched them: the orchestrator first. Each takes
    longer than the dashboard waits for an answer, so they come back in an operation the page follows."""
    with ctx.lock:
        try:
            record = store.read_json(paused_path(ctx))
        except FileNotFoundError as exc:
            raise Refused("paused", "the factory is not paused") from exc
        sessions = read(ctx)
        paused = [sessions[i] for i in record["sessions"] if i in sessions and sessions[i].state != RETIRED]
        settling = [s.name for s in paused if s.state == models.BENCH_PENDING]
        if settling:
            raise Refused("settling", f"the pause is still settling ({', '.join(settling)}); resume in a minute")
        permits.check_vitals(ctx, severe=True)
        permits.check_daemon(ctx)
        back = [s for s in paused if s.state == models.DEPRIORITIZED]
        for s in back:
            move(ctx, s, models.REVIVING, OWNER, "resuming with the factory")
        registry.write(ctx.places, sessions)
        paused_path(ctx).unlink()

    def bring_all() -> str:
        failed = {s.name: why for s in back if (why := bring_back(ctx, s.id, OWNER))}
        ctx.post_event(inbox.RESUMED, {"sessions": [s.id for s in back], "failed": sorted(failed)})
        if failed:
            raise ops.StepError("; ".join(f"{name} stays on the bench: {why}" for name, why in failed.items()))
        return f"back: {', '.join(s.name for s in back)}" if back else "no agent was on the bench"

    return ok(
        op=ops.launch(ctx, operations.RESUME, [("Bring back every benched agent", bring_all)]),
        resuming=[s.name for s in back],
    )
