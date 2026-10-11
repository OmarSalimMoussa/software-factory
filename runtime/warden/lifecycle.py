"""Every session's lifecycle: start, bench, revive, retire, and the owner's stop, pause and resume.

States are working, waiting, bench-pending, deprioritized, reviving and retired. A pending state is written before
the warden acts, so the crash watch and the backstop never misread a session in between. Retired is final: nothing
revives or resumes it. Only the owner retires a deprioritized session; a bench makes room only for a task that
outranks the one it replaces. Each move is a line in history.jsonl with who made it and why.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from runtime.queue import statuses
from runtime.shared import models, store
from runtime.shared.models import RETIRED, WAITING, WORKING, RecordError, Session, Task
from runtime.shared.protocol import Refused, ok
from runtime.slots import leases
from runtime.warden import backstop, doing, inbox, launch, ops, outage, permits, registry
from runtime.warden.context import Context
from runtime.warden.launch import LaunchError
from runtime.warden.permits import CAP, DESIGN, FAMILY, IMPL, ORCH, SEVERE, actor

log = logging.getLogger(__name__)
OWNER, WARDEN = "owner", "warden"
LIMITS = ("qa", "critic", "check_runs")
FIRST_LINE = "Start your loop."
PAUSED = {"message": "The factory is paused; every session keeps its context.", "refresh": "all"}
REVIVED = "The warden brought you back: carry on with your task from your branch."
_NAME = re.compile(r"[^a-z0-9]+")


def paused_path(ctx: Context) -> Path:
    return ctx.places.state / "paused.json"


def read(ctx: Context) -> dict[str, Session]:
    try:
        return registry.read(ctx.places)
    except (store.StoreError, RecordError) as exc:
        raise Refused("state", f"the registry cannot be read ({exc}); the warden's state needs a look") from exc


def find(ctx: Context, sessions: dict[str, Session], ref: str) -> Session:
    """A session by id (a start's pending id too), or by name: the live one, else the latest retired one."""
    if ref.startswith(launch.PENDING) and ref not in sessions:
        ref = next((r["session"] for r in store.read_jsonl(ctx.places.history) if r.get("was") == ref), ref)
    if ref in sessions:
        return sessions[ref]
    named = sorted((s for s in sessions.values() if s.name == ref), key=lambda s: (s.state != RETIRED, s.started))
    if not named:
        raise Refused("session", f"no session {ref}")
    return named[-1]


def _by(ctx: Context, role: str) -> dict[str, str]:
    return {"family": FAMILY[role], "started_by": actor(ctx)}


def _text(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise Refused("args", f"name the {key}")
    return value.strip()


def move(ctx: Context, s: Session, state: str, by: str, why: str) -> None:
    s.state, s.since, s.note = state, ctx.clock.iso(), why
    store.append_jsonl(ctx.places.history, {"at": s.since, "session": s.id, "name": s.name, "state": state,
                                            "retired": s.retired, "by": by, "why": why})  # fmt: skip


def retire(ctx: Context, tree: list[Session], root: Session, kind: str, by: str, why: str) -> list[Session]:
    """Retire ``root`` as ``kind`` and the rest of ``tree`` with it; checks every one before changing any."""
    tree = [s for s in tree if s.state != RETIRED]
    for s in tree:
        if s.state in (models.DEPRIORITIZED, models.BENCH_PENDING, models.REVIVING) and by != OWNER:
            raise Refused("deprioritized", f"{s.name} is {s.state} and comes back with its context; only the owner "
                                           "retires it")  # fmt: skip
    for s in tree:
        s.retired, s.retired_by, s.ended = (kind if s is root else models.WITH_PARENT), by, ctx.clock.iso()
        move(ctx, s, RETIRED, by, why)
    return tree


def after_retire(ctx: Context, retired: list[Session]) -> None:
    """Stop what still runs, and send an unfinished task back to Proposed next."""
    running = backstop.processes()
    for s in retired:
        if s.id in running:
            try:
                ctx.launcher.stop_session(s.id)
            except LaunchError as exc:
                log.warning("stop of retired session %s failed (%s); the backstop retries", s.id, type(exc).__name__)
        unfinished = s.task and not s.parent and s.role in CAP and s.retired != models.FINISHED
        task = permits.read_task(ctx, s.task) if unfinished else None
        if task is not None and task.status in statuses.OPEN:
            if s.retired_by == OWNER:
                event, by = "stop", OWNER
            else:
                event, by = ("crashed" if s.retired == models.CRASHED else "stopped"), WARDEN
            permits.move_task(ctx, s.task, "proposed" if task.why and task.picture else "queued", event, by)


def retire_session(ctx: Context, ref: str, kind: str, by: str) -> list[Session]:
    """Retire one session and everything under it, as ``by``; the sessions retired (none when it already was)."""
    with ctx.lock:
        sessions = read(ctx)
        target = find(ctx, sessions, ref)
        retired = retire(ctx, registry.subtree(sessions, target.id), target, kind, by, f"{kind} by the {by}")
        registry.write(ctx.places, sessions)
    after_retire(ctx, retired)
    return retired


# The sessions a launch (a resume, a restart) is under way for. A launch runs outside ctx.lock, which every request
# waits on, and may take factory.command_seconds; it is claimed under the lock, so the crash watch never reads the
# moment between a stop and a resume as a crash, and no second launch of the same session starts.
_launching: set[str] = set()


def claim_launch(ctx: Context, sid: str) -> bool:
    """Claim ``sid`` for one launch; False when another holds it. The caller holds ctx.lock."""
    if sid in _launching:
        return False
    _launching.add(sid)
    return True


def launching(sid: str) -> bool:
    return sid in _launching


def release_launch(ctx: Context, sid: str) -> None:
    with ctx.lock:
        _launching.discard(sid)


def restart(ctx: Context, s: Session, message: str) -> None:
    """Stop the session's process and resume it with ``message``: for one that went silent. Raises LaunchError when
    another launch of it is under way."""
    with ctx.lock:
        if not claim_launch(ctx, s.id):
            raise LaunchError(f"{s.name} is being launched already")
    try:
        if s.id in backstop.processes():
            ctx.launcher.stop_session(s.id)
        launch.resume(ctx, s, message)
    finally:
        release_launch(ctx, s.id)


def deliver(ctx: Context, sid: str, message: str, by: str) -> None:
    """Hand a working or waiting session ``message`` as its next turn, and a waiting one works again. A running
    process reads it as soon as its turn ends, busy or idle, so no turn is cut short; one whose process is gone is
    resumed with it (claimed, so the crash watch never resumes it too). Raises LaunchError when it did not arrive or
    the warden is launching it already, Refused when the session is not working or waiting."""
    with ctx.lock:
        s = read(ctx).get(sid)
        if s is None or s.state not in (WORKING, WAITING):
            raise Refused("state", f"{s.name if s else sid} is {s.state if s else 'unknown'}; nothing to hand over")
        running = s.id in backstop.processes()
        if not running and not claim_launch(ctx, sid):
            raise LaunchError(f"the warden is bringing {s.name} back right now: send it again in a minute")
    if running:
        ctx.launcher.message_session(s.id, message)
    else:
        try:
            launch.resume(ctx, s, message)
        finally:
            release_launch(ctx, sid)
    with ctx.lock:
        sessions = read(ctx)
        current = sessions[sid]
        if current.state == WAITING:
            move(ctx, current, WORKING, by, "the owner's word reached it")
            registry.write(ctx.places, sessions)


def start(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    role = _text(args, "role")
    if role == ORCH:
        if actor(ctx) != OWNER:
            raise Refused("role", "only the owner starts the orchestrator")
        return owner_start_orchestrator(ctx, args, data)
    if ctx.caller.role != ORCH:
        raise Refused("role", "only the orchestrator starts implementers and designers; start your own claude yourself")
    note = str(args.get("note", ""))
    if len(note.encode()) > ctx.settings().get_int("queue.max_upload_kib") * 1024:
        raise Refused("args", "the note is too long; put the detail in the task")
    s = start_run(ctx, role, permits.read_task(ctx, _text(args, "task")), note, args.get("outage") is True)
    return ok(session=s.id, name=s.name, model=s.model, rung=s.rung)


def first_prompt(task: Task, held: str, note: str) -> str:
    """A run's first prompt: its task's essentials, since queue/ is not in its worktree, and the rest a command away."""
    body = f"\n\n{task.body.strip()}" if task.body.strip() else ""
    rest = f"\n\n`factory queue task show {task.id}` gives its log, its requests and its spec."
    return f"Task: {task.id}, {task.title}{body}{rest}{held}\n\n{note}".strip()


def brief_path(ctx: Context, task_id: str) -> Path:
    """The brief the orchestrator wrote when it proposed the task, which its run starts with."""
    return ctx.places.state / "briefs" / f"{task_id}.md"


def start_run(ctx: Context, role: str, task: Task, note: str, outage_fix: bool) -> Session:
    """Start an implementer or designer on ``task`` under its permit, as the orchestrator asked or the warden's own
    scheduling pass decided; raises Refused naming the first rule it breaks. An implementer gets a slot when one is
    free, as a revived one does; its first message says which."""
    permits.check_not_factory(ctx)
    if not note and brief_path(ctx, task.id).is_file():
        note = brief_path(ctx, task.id).read_text(encoding="utf-8")
    with ctx.lock:
        permits.check_daemon(ctx)
        sessions = read(ctx)
        permit = permits.check_start(ctx, sessions, role, task, outage_fix)
        permits.check_vitals(ctx, task.severity == SEVERE)
        if permit.bypass and permits.cap_full(ctx.settings(), sessions.values(), role):
            victim = make_room(ctx, sessions, task)
            raise Refused("cap", f"making room for the outage fix: {victim.name} is being benched; start again soon")
        permits.check_cap(ctx.settings(), sessions.values(), role)
        permits.check_revive_order(ctx, sessions, role, task)
        name = ("impl-" if role == IMPL else "design-") + _NAME.sub("-", task.id.lower()).strip("-")
        fields = {"name": name, "role": role, "model": permit.model, "effort": permit.effort, "task": task.id,
                  "rung": permit.rung, **_by(ctx, role)}  # fmt: skip
        slot = _claim_at_start(ctx, name, role, task.id)
        held = f"\n\nSlot {slot} is yours: bring it up with factory slot up." if slot is not None else ""
        try:
            s = launch.start_session(ctx, sessions, fields, first_prompt(task, held, note), worktree=name)
        except Refused:
            if slot is not None:
                permits.free_slot(ctx, slot)
            raise
        if permit.bypass:
            outage.mark_bypass(ctx, task.id, s.id)
    if task.status != "building":
        permits.move_task(ctx, task.id, "designing" if role == DESIGN else "building",
                          "outage" if permit.bypass else "start")  # fmt: skip
    return s


def _claim_at_start(ctx: Context, name: str, role: str, task_id: str) -> int | None:
    if role != IMPL:
        return None
    try:
        return leases.claim(ctx, name, f"started for {task_id}")
    # telemetry: expected — every slot is held; the implementer claims one when it frees
    except Refused:
        return None


def owner_start_orchestrator(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    permits.check_vitals(ctx, severe=True)
    permits.check_not_factory(ctx)
    with ctx.lock:
        if paused_path(ctx).exists():
            raise Refused("paused", "the factory is paused; resume it instead")
        permits.check_daemon(ctx)
        sessions = read(ctx)
        current = [s for s in sessions.values() if s.role == ORCH and s.state != RETIRED]
        if current:
            raise Refused("one-orchestrator", f"the orchestrator is already {current[0].state}")
        model, effort = ctx.settings().model("orchestrator")
        fields = {"name": ORCH, "role": ORCH, "model": model, "effort": effort, "task": "", **_by(ctx, ORCH)}
        s = launch.start_session(ctx, sessions, fields, FIRST_LINE, worktree="")
    ctx.post_event(inbox.ORCHESTRATOR_STARTED, {"session": s.id})
    return ok(session=s.id, name=s.name)


def stop(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    if actor(ctx) == OWNER:
        return owner_stop(ctx, args, data)
    why = str(args.get("why", "")).strip() or "stopped by the orchestrator"
    with ctx.lock:
        sessions = read(ctx)
        target = find(ctx, sessions, _text(args, "session"))
        if target.role == ORCH:
            raise Refused("orchestrator", "only the owner stops the orchestrator, which stops the factory")
        if target.state == RETIRED:
            raise Refused("retired", f"{target.name} is already retired")
        retired = retire(ctx, registry.subtree(sessions, target.id), target, models.STOPPED, ORCH, why)
        registry.write(ctx.places, sessions)
    after_retire(ctx, retired)
    return ok(retired=[s.name for s in retired])


def owner_stop(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """Retire a session and all under it; the orchestrator takes the whole factory with it, and needs the code."""
    with ctx.lock:
        sessions = read(ctx)
        target = find(ctx, sessions, _text(args, "session"))
        if target.state == RETIRED:
            raise Refused("retired", f"{target.name} is already retired")
        tree = registry.subtree(sessions, target.id)
        if target.role == ORCH:
            from runtime.warden.codes import check_code

            check_code(ctx, args.get("code"), lock_exempt=True)  # a code lock never blocks stopping the factory
            tree = [target, *(s for s in sessions.values() if s is not target)]
        retired = retire(ctx, tree, target, models.STOPPED, OWNER, "stopped by the owner")
        registry.write(ctx.places, sessions)
        if target.role == ORCH:
            paused_path(ctx).unlink(missing_ok=True)
    after_retire(ctx, retired)
    ctx.post_event(inbox.STOPPED, {"session": target.id, "name": target.name, "retired": [s.id for s in retired]})
    return ok(retired=[s.name for s in retired])


def resume(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """Hand a session a message: it reads it when its current turn ends, and one whose process is gone resumes."""
    message = _text(args, "message")
    with ctx.lock:
        permits.check_daemon(ctx)
        s = find(ctx, read(ctx), _text(args, "session"))
        if s.state == RETIRED:
            raise Refused("retired", f"{s.name} is retired, and a retired session can never be resumed")
        if s.state not in (WORKING, WAITING):
            raise Refused("state", f"{s.name} is {s.state}; a deprioritized session comes back with factory revive")
    try:
        deliver(ctx, s.id, message, actor(ctx))
    except LaunchError as exc:
        raise Refused("resume", f"{s.name} did not get the message: {exc}") from exc
    return ok(name=s.name)


def extended_once(s: Session, limit: str) -> bool:
    """Whether the orchestrator used its one extension of ``limit`` on ``s``."""
    return bool(s.extensions.get(f"{limit}:orchestrator"))


def raise_limit(ctx: Context, sessions: dict[str, Session], s: Session, limit: str, by_orchestrator: bool) -> int:
    """Add rounds.extend_by to ``s``'s ``limit``; the caller holds ctx.lock and writes the registry. The new extra."""
    s.extensions[limit] = s.extensions.get(limit, 0) + ctx.settings().get_int("rounds.extend_by")
    if by_orchestrator:
        s.extensions[f"{limit}:orchestrator"] = 1
    return s.extensions[limit]


def extend(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """One more allowance of a limit: the orchestrator's once per session and limit, the owner's any time. The
    orchestrator's second ask puts the task on the owner's decide card."""
    from runtime.queue import cards  # cards moves sessions through this module

    limit, who = _text(args, "limit"), actor(ctx)
    if limit not in LIMITS:
        raise Refused("args", f"extend one of {', '.join(LIMITS)}")
    with ctx.lock:
        sessions = read(ctx)
        s = find(ctx, sessions, _text(args, "session"))
        if s.state == RETIRED:
            raise Refused("retired", f"{s.name} is retired")
        if who != OWNER and extended_once(s, limit):
            if cards.spent(ctx, s, limit, f"{s.name} used up its {limit} again after one extension"):
                raise Refused("extend", f"{s.name}'s {limit} was extended once already; the owner decides now, on "
                                        "the card on Home")  # fmt: skip
            raise Refused("extend", f"{s.name}'s {limit} was extended once already, and its task is not running")
        extra = raise_limit(ctx, sessions, s, limit, by_orchestrator=who != OWNER)
        registry.write(ctx.places, sessions)
    if who == OWNER:
        ctx.post_event(inbox.LIMIT_EXTENDED, {"session": s.id, "name": s.name, "limit": limit, "extra": extra})
    return ok(name=s.name, limit=limit, extra=extra)


def set_doing(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    text = doing.clean(str(args.get("text", "")))
    with ctx.lock:
        sessions = read(ctx)
        s = sessions.get(ctx.caller.session)
        if s is None or s.state == RETIRED or ctx.caller.agent_id:
            raise Refused("session", "only a live session the warden started says what it is doing")
        s.doing = text
        registry.write(ctx.places, sessions)
    return ok(doing=doing.describe(ctx.places, ctx.settings(), s))


# ---- bench and revive ----------------------------------------------------------------------------------------------


def begin_bench(ctx: Context, s: Session, by: str, why: str, benched_for: str) -> None:
    """The guard reads bench-pending and lets the session only commit and run factory until the grace ends; the
    crash watch then stops it (crashwatch.finish_benches). ``benched_for`` is the task it makes room for, or
    models.PAUSED."""
    s.benched_for = benched_for
    move(ctx, s, models.BENCH_PENDING, by, why)


def make_room(ctx: Context, sessions: dict[str, Session], task: Task) -> Session:
    choices = permits.benchable(ctx, sessions, IMPL, task.id)
    if not choices:
        raise Refused("cap", "no implementer can make room for the outage fix: each outranks it or holds a merge turn")
    begin_bench(ctx, choices[0], WARDEN, f"benched for the outage fix {task.id}", task.id)
    registry.write(ctx.places, sessions)
    return choices[0]


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
        launch.resume(ctx, s, REVIVED + rebuild + (f"\n\n{note}" if note else ""))
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
    if s.task and permits.read_task(ctx, s.task).status == "deprioritized":
        permits.move_task(ctx, s.task, "designing" if s.role == DESIGN else "building", "revive")
    return ""


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


# ---- the owner's pause and resume ----------------------------------------------------------------------------------


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
        op_id = ops.start(ctx, "pause", ["Ask every session to commit and stop", *(f"Bench {s.name}" for s in ranked)])
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
    """Bring back every session the pause benched, in the order it benched them: the orchestrator first."""
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
    failed = {s.name: why for s in back if (why := bring_back(ctx, s.id, OWNER))}
    ctx.post_event(inbox.RESUMED, {"sessions": [s.id for s in back], "failed": sorted(failed)})
    return ok(resumed=[s.name for s in back if s.name not in failed], failed=failed)
