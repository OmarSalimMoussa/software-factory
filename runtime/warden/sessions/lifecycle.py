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
from runtime.shared.models import PENDING, RETIRED, WAITING, WORKING, RecordError, Session, Task
from runtime.shared.protocol import Refused, ok
from runtime.slots import leases
from runtime.warden import inbox
from runtime.warden.context import Context
from runtime.warden.delivery import outage
from runtime.warden.sessions import backstop, doing, launch, permits, registry
from runtime.warden.sessions.launch import LaunchError
from runtime.warden.sessions.permits import CAP, DESIGN, FAMILY, IMPL, ORCH, SEVERE, actor

log = logging.getLogger(__name__)
OWNER, WARDEN = "owner", "warden"
FIRST_LINE = "Start your loop."
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
    if ref.startswith(PENDING) and ref not in sessions:
        ref = next((r["session"] for r in store.read_jsonl(ctx.places.history) if r.get("was") == ref), ref)
    if ref in sessions:
        return sessions[ref]
    named = sorted((s for s in sessions.values() if s.name == ref), key=lambda s: (s.state != RETIRED, s.started))
    if not named:
        raise Refused("session", f"no session {ref}")
    return named[-1]


def _by(ctx: Context, role: str) -> dict[str, str]:
    return {"family": FAMILY[role], "started_by": actor(ctx)}


def text_arg(args: dict[str, Any], key: str) -> str:
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


def end_children(ctx: Context, sessions: dict[str, Session], s: Session, why: str) -> None:
    """A session whose process ended took its subagents with it: their rows retire, so none holds a tester's place,
    and resumed, it starts them again. The caller holds ctx.lock and writes the registry."""
    running = [c for c in registry.children(sessions, s.id) if c.state in (WORKING, WAITING)]
    if running:
        retire(ctx, running, s, models.WITH_PARENT, WARDEN, why)


def after_retire(ctx: Context, retired: list[Session]) -> None:
    """Stop what still runs, and send an unfinished task back to Proposed next."""
    if not retired:
        return
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
        sessions = read(ctx)
        end_children(ctx, sessions, sessions.get(s.id, s), "its parent's process ended")
        registry.write(ctx.places, sessions)
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
        if not running:
            sessions = read(ctx)
            end_children(ctx, sessions, sessions[sid], "its parent's process ended")
            registry.write(ctx.places, sessions)
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
    role = text_arg(args, "role")
    if role == ORCH:
        if actor(ctx) != OWNER:
            raise Refused("role", "only the owner starts the orchestrator")
        return owner_start_orchestrator(ctx, args, data)
    if ctx.caller.role != ORCH:
        raise Refused("role", "only the orchestrator starts implementers and designers; start your own claude yourself")
    note = str(args.get("note", ""))
    if len(note.encode()) > ctx.settings().get_int("queue.max_upload_kib") * 1024:
        raise Refused("args", "the note is too long; put the detail in the task")
    s = start_run(ctx, role, permits.read_task(ctx, text_arg(args, "task")), note, args.get("outage") is True)
    return ok(session=s.id, name=s.name, model=s.model, rung=s.rung)


def first_prompt(task: Task, held: str, note: str) -> str:
    """A run's first prompt: its task's essentials, since queue/ is not in its worktree, and the rest a command away."""
    body = f"\n\n{task.body.strip()}" if task.body.strip() else ""
    rest = f"\n\n`factory queue task show {task.id}` gives its log, its requests and its spec."
    return f"Task: {task.id}, {task.title}{body}{rest}{held}\n\n{note}".strip()


def brief_path(ctx: Context, task_id: str) -> Path:
    """The brief the orchestrator wrote when it proposed the task, which its run starts with."""
    return ctx.places.state / "briefs" / f"{task_id}.md"


def held_path(ctx: Context, task_id: str) -> Path:
    """A word for the task's run (a revert's), kept until a session of it takes it: handed to a working one, or
    carried by the run's next start or revive when none is working, or it did not arrive. Only its latest is kept."""
    return ctx.places.state / "briefs" / f"{task_id}.held.md"


def held_word(ctx: Context, task_id: str) -> str:
    path = held_path(ctx, task_id)
    return path.read_text(encoding="utf-8").strip() if task_id and path.is_file() else ""


def took_held(ctx: Context, task_id: str, sent: str) -> str:
    """The held word went out as ``sent``: it is gone, unless a newer one was held meanwhile, which is returned to be
    handed over. The caller holds ctx.lock, as the writer of a held word does."""
    late = held_word(ctx, task_id)
    if late != sent:
        return late
    if task_id:
        held_path(ctx, task_id).unlink(missing_ok=True)
    return ""


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
        took = f"\n\nSlot {slot} is yours: bring it up with factory slot up." if slot is not None else ""
        held = held_word(ctx, task.id)  # read and dropped under the lock its writer holds
        said = "\n\n".join(part for part in (note.strip(), held) if part)
        try:
            s = launch.start_session(ctx, sessions, fields, first_prompt(task, took, said), worktree=name)
        except Refused:
            if slot is not None:
                permits.free_slot(ctx, slot)
            raise
        if permit.bypass:
            outage.mark_bypass(ctx, task.id, s.id)
        took_held(ctx, task.id, held)
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
        target = find(ctx, sessions, text_arg(args, "session"))
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
        target = find(ctx, sessions, text_arg(args, "session"))
        if target.state == RETIRED:
            raise Refused("retired", f"{target.name} is already retired")
        tree = registry.subtree(sessions, target.id)
        if target.role == ORCH:
            from runtime.warden.owner.codes import check_code

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
    message = text_arg(args, "message")
    with ctx.lock:
        permits.check_daemon(ctx)
        s = find(ctx, read(ctx), text_arg(args, "session"))
        if s.state == RETIRED:
            raise Refused("retired", f"{s.name} is retired, and a retired session can never be resumed")
        if s.state not in (WORKING, WAITING):
            raise Refused("state", f"{s.name} is {s.state}; the warden brings a benched session back itself when a "
                                   "place frees")  # fmt: skip
    try:
        deliver(ctx, s.id, message, actor(ctx))
    except LaunchError as exc:
        raise Refused("resume", f"{s.name} did not get the message: {exc}") from exc
    return ok(name=s.name)


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


# ---- bench ---------------------------------------------------------------------------------------------------------


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
