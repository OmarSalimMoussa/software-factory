"""Tasks as the warden keeps them: read, moved along the status table, given facts, ordered and scored.

Every status change goes through ``transition``: the table check, the task file and its log line, and the proposal
places and approvals that follow from the new status. Other parts of the warden move a task with ``move``.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from runtime.queue import approvals, documents, formats, goal, intake, proposals, route, statuses
from runtime.queue.book import Queue, task_rel
from runtime.queue.score import score
from runtime.queue.statuses import OWNER, WARDEN
from runtime.shared import store
from runtime.shared.models import RETIRED, Request, Task
from runtime.shared.order import Ranking, ordered
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused
from runtime.warden import checks, inbox
from runtime.warden.context import Context
from runtime.warden.sessions import registry

LANDED = frozenset({"merged", "checked", "on-staging", "promoted", "closed"})
SCORED = frozenset({"queued", "parked", "proposed"})
REVERTED = "reverted"  # the event that takes a landed task back to its builder
BLOCKED = "blocked"
ON_HOLD = "on-hold"
_FACTS: dict[str, type] = {
    "title": str,
    "goal_item": str,
    "severity": str,
    "surfaces": list,
    "cause": str,
    "slices": int,
    "owner_class": str,
    "after": list,
    "branch": str,
}


def actor(ctx: Context) -> str:
    return OWNER if ctx.caller.kind in ("owner", "dashboard") else ctx.caller.role


def read(ctx: Context, task_id: str) -> Task:
    """The task; FileNotFoundError when there is none."""
    return Queue.of(ctx).task(task_id)


def move(ctx: Context, task_id: str, to: str, event: str, by: str = WARDEN, text: str = "", **fields: Any) -> Task:
    """Move a task along the status table for ``by`` (the warden itself unless named), setting ``fields`` with it."""
    queue = Queue.of(ctx)
    with ctx.lock, queue.lock():
        return transition(ctx, queue, queue.task(task_id), to, event, by, text or f"{event}: now {to}", **fields)


def transition(
    ctx: Context,
    queue: Queue,
    task: Task,
    to: str,
    event: str,
    by: str,
    text: str,
    pictures: Mapping[str, bytes] | None = None,
    commit: str = "",
    **fields: Any,
) -> Task:
    """Write the task with its new status and ``fields``; the caller holds ctx.lock and the queue lock."""
    from runtime.queue import cards, turn  # both move tasks through this module

    moved = dataclasses.replace(statuses.move(task, to, event, by), **fields)
    if to == "turn" and turn.holder(ctx.places) is not None:
        raise Refused("one-turn", "another task holds the merge turn; the next one goes when it merges")
    queue.write({task_rel(moved.id): formats.dump_task(moved)}, pictures)
    extra = {"from": task.status, "to": to, **({"commit": commit} if commit else {})}
    queue.log(moved.id, ctx.clock.iso(), by, event, text, **extra)
    follow(ctx, task, moved)
    if task.status == BLOCKED and to != BLOCKED:
        cards.unblocked(ctx, moved.id, to)
    return moved


def follow(ctx: Context, before: Task, after: Task) -> None:
    """Keep the proposal places, the approved order, the approval and the implementer's merge turn in step with a
    task's new status."""
    from runtime.queue import turn  # the turn moves tasks through this module

    if (before.status == "turn") != (after.status == "turn"):
        _hold_turn(ctx, after.id, after.status == "turn")
    if after.status == "turn" and before.status != "turn":
        checks.new_turn(ctx, after.id)
        turn.held(ctx, after)
    if before.status == "turn" and after.status != "turn":
        turn.released(ctx, after.id)
    found = proposals.read(ctx.places)
    if after.status != "proposed" and proposals.remove(found, after.id) and before.status == "proposed":
        ctx.post_event(inbox.SLOT_FREE, {"task": after.id})
    if after.status == "approved" and after.id not in found.approved:
        found.approved.append(after.id)
    if after.status != "approved":
        found.approved = [t for t in found.approved if t != after.id]
    if after.status == "proposed" and after.id not in found.open:
        proposals.place_top(found, after.id)
    if after.status in ("proposed", "queued", "parked", "closed"):
        approvals.void(ctx.places, after.id)
    proposals.write(ctx.places, found)
    builds = proposals.designed_builds(ctx.places)
    designed = before.status in ("designing", "review") and after.status == "building"
    if designed != (after.id in builds):
        proposals.set_designed_builds(ctx.places, [*builds, after.id] if designed else
                                      [t for t in builds if t != after.id])  # fmt: skip


def _hold_turn(ctx: Context, task_id: str, held: bool) -> None:
    """The task's implementer holds its merge turn while the task does, so no bench pushes it out."""
    sessions = registry.read(ctx.places)
    mine = [s for s in sessions.values()
            if s.task == task_id and s.role == statuses.IMPL and not s.parent and s.state != RETIRED]  # fmt: skip
    for s in mine:
        s.merge_turn = held
    if mine:
        registry.write(ctx.places, sessions)


def set_status(ctx: Context, task_id: str, to: str, event: str, note: str, after: str = "") -> Task:
    """The orchestrator's move of a task (a merge turn, a spec it approved, a close); refused off the table. A revert
    goes where the warden finds its implementer, and a block or a hold benches it (runtime/queue/turn.py)."""
    from runtime.queue import turn  # the turn moves tasks through this module

    queue = Queue.of(ctx)
    with ctx.lock, queue.lock():
        task = queue.task(task_id)
        if not event:
            # The table names the one event for most moves; a close says which kind it is.
            found = statuses.events(task.status, to, actor(ctx))
            if len(found) != 1:
                raise Refused("task-status", f"{task_id} {task.status} → {to} needs --event: one of {found or 'none'}")
            event = found[0]
        if after and event != ON_HOLD:
            raise Refused("task-status", "--after names the task a hold waits for, and goes only with on-hold")
        if event not in (REVERTED, BLOCKED, ON_HOLD):
            moved = transition(ctx, queue, task, to, event, actor(ctx), note or f"{event}: now {to}")
            if event == "spec-approved":
                documents.approve_spec(ctx, queue, task_id)
            return moved
    if event in (BLOCKED, ON_HOLD) and to != event:
        raise Refused("task-status", f"--event {event} moves {task_id} to {event}, nowhere else")
    if event == BLOCKED:
        return turn.block(ctx, task_id, note)
    if event == ON_HOLD:
        return turn.hold(ctx, task_id, note, after)
    if to != "building":
        raise Refused("task-status", f"a revert sends {task_id} back to building; the warden finds who builds it")
    return turn.send_back(ctx, task_id, note)


def goal_ranks(ctx: Context) -> dict[str, int]:
    """Each open goal item's rank in GOAL.md, by its name."""
    settings = ctx.settings()
    try:
        items = goal.read(Path(settings.get_str("repo.path")), settings.get_int("queue.max_upload_kib") * 1024)
    except goal.GoalError as exc:
        raise Refused("goal", f"{exc}; only a person edits GOAL.md") from exc
    return goal.ranks(items)


def scored(ctx: Context, queue: Queue, task: Task, goals: Mapping[str, int]) -> Task:
    """The task scored as of the last rescore: a score's age part is fixed when it is written, so every waiting
    task's is measured from the same time, and the order reads them side by side."""
    at = rescored_at(ctx.places)
    return _scored_at(ctx, queue, task, goals, ctx.clock.now() if at is None else at)


def _scored_at(ctx: Context, queue: Queue, task: Task, goals: Mapping[str, int], at: float) -> Task:
    reqs = [queue.request(r) for r in task.requests]
    return dataclasses.replace(task, score=score(task, reqs, goals, at, ctx.settings()))


def _rescored_path(places: Places) -> Path:
    return places.state / "rescored.json"


def rescored_at(places: Places) -> float | None:
    """When every waiting task was last scored at once; None before the first time."""
    raw = store.read_json_or(_rescored_path(places), {"at": None})
    at = raw.get("at") if isinstance(raw, dict) else "missing"
    if at is not None and not isinstance(at, int | float):
        raise store.StoreError(f"{_rescored_path(places)}: not a time")
    return at


def facts_from(args: Mapping[str, Any], allowed: frozenset[str] | None = None) -> dict[str, Any]:
    """The task facts in ``args``, each of its kind; anything else is refused."""
    keys = allowed if allowed is not None else frozenset(_FACTS)
    unknown = [k for k in args if k not in keys]
    if unknown:
        raise Refused("facts", f"not task facts: {', '.join(unknown)}")
    found: dict[str, Any] = {}
    for key, value in args.items():
        kind = _FACTS[key]
        if key == "slices" and value is None:
            found[key] = None
        elif kind is int and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise Refused("facts", f"{key} is a whole number from 0")
        elif kind is list and not (isinstance(value, list) and all(isinstance(v, str) for v in value)):
            raise Refused("facts", f"{key} is a list of words")
        elif kind is str and not isinstance(value, str):
            raise Refused("facts", f"{key} is text")
        else:
            found[key] = [v.strip() for v in value] if kind is list else value
    return found


def new_task(ctx: Context, args: Mapping[str, Any], body: str) -> Task:
    """The fast lane: one task from the requests intake debugged, written at once and scored."""
    queue = Queue.of(ctx)
    facts = facts_from({k: v for k, v in args.items() if k != "requests"})
    request_ids = args.get("requests")
    if not isinstance(request_ids, list) or not request_ids or not all(isinstance(r, str) for r in request_ids):
        raise Refused("facts", "a task links at least one request: --requests r-…")
    goals = goal_ranks(ctx)
    with ctx.lock, queue.lock():
        reqs = [_waiting(queue, rid) for rid in request_ids]
        now = ctx.clock.iso()
        draft = Task(
            id=queue.next_task_id(),
            title="",
            requests=list(request_ids),
            goal_item="",
            severity="",
            surfaces=[],
            status=statuses.NEW,
            created=now,
            body=body.strip(),
        )
        draft = dataclasses.replace(draft, **facts)
        boosted = intake.boosts(ctx)
        if any(r.id in boosted for r in reqs):
            draft.boosted_at = now
        task = scored(ctx, queue, statuses.move(draft, "queued", "fast-lane", actor(ctx)), goals)
        changes = {task_rel(task.id): formats.dump_task(task)}
        for req in reqs:
            req.outcome = f"task {task.id}"
            changes[f"requests/{req.inbox}/{req.id}.md"] = formats.dump_request(req)
        queue.write(changes)
        queue.log(task.id, now, actor(ctx), "fast-lane", f"made from {', '.join(request_ids)}", to="queued")
        intake.set_boosts(ctx, {k: v for k, v in boosted.items() if k not in request_ids})
    return task


def _waiting(queue: Queue, request_id: str) -> Request:
    try:
        req = queue.request(request_id)
    except FileNotFoundError as exc:
        raise Refused("request", f"no request {request_id}") from exc
    if req.outcome:
        raise Refused("request", f"{request_id} already has its outcome: {req.outcome}")
    return req


def set_facts(ctx: Context, task_id: str, args: Mapping[str, Any]) -> Task:
    """The orchestrator's confirmed facts; a changed title voids an approval, a higher severity a Not now."""
    queue = Queue.of(ctx)
    facts = facts_from(args)
    if not facts:
        raise Refused("facts", "name at least one fact to set")
    goals = goal_ranks(ctx)
    with ctx.lock, queue.lock():
        task = queue.task(task_id)
        if task.status == "closed":
            raise Refused("task-status", f"{task_id} is closed")
        changed = dataclasses.replace(task, **facts)
        if _severity_rank(changed.severity) < _severity_rank(task.severity):
            changed.not_now = False
        if changed.status in SCORED:
            changed = scored(ctx, queue, changed, goals)
        # A proposal stands on the route its facts named, or on a reason to differ given against that route: facts
        # that route elsewhere send it back, to be proposed again on them. Only a proposal is routed here: a queued
        # task may still lack the facts a route needs.
        rerouted = None
        if task.status == "proposed":
            routed = route.route(changed, ctx.settings())
            rerouted = routed if routed != route.route(task, ctx.settings()) else None
        queue.write({task_rel(task_id): formats.dump_task(changed)})
        said = ", ".join(f"{k} {v}" for k, v in facts.items())
        queue.log(task_id, ctx.clock.iso(), actor(ctx), "facts", said)
        approval = approvals.current(ctx.places, task_id)
        if approval is not None and not approvals.matches(approval, changed):
            changed = transition(ctx, queue, changed, "proposed", "void", WARDEN, "what was approved changed")
        if rerouted is not None:
            changed = transition(ctx, queue, changed, "queued", "void", WARDEN,
                                 f"its facts route it to rung {rerouted} now: propose it again")  # fmt: skip
    return changed


def _severity_rank(severity: str) -> int:
    return ("severe", "major", "minor").index(severity) if severity in ("severe", "major", "minor") else 3


def next_up(ctx: Context) -> dict[str, Any]:
    """The queue in its one order, with the tasks skipped for now and why, and any file that cannot be read."""
    queue = Queue.of(ctx)
    tasks, broken = queue.tasks()
    by_id = {t.id: t for t in tasks}
    ready: list[Task] = []
    skipped: list[dict[str, str]] = []
    for task in (t for t in tasks if t.status == "queued"):
        waits = [a for a in task.after if a not in by_id or by_id[a].status not in LANDED]
        if task.not_now:
            skipped.append({"task": task.id, "why": "the owner said not now"})
        elif waits:
            skipped.append({"task": task.id, "why": f"waits for {', '.join(waits)}"})
        else:
            ready.append(task)
    ranking = Ranking(proposals.read(ctx.places).approved, goal_ranks(ctx))
    rows = [
        {
            "task": t.id,
            "title": t.title,
            "severity": t.severity,
            "goal_item": t.goal_item,
            "score": t.score,
            "boosted": bool(t.boosted_at),
            "surfaces": t.surfaces,
            "slices": t.slices,
        }
        for t in ordered(ready, ranking)
    ]
    return {"next": rows, "skipped": skipped, "unreadable": [dataclasses.asdict(b) for b in broken]}


def merge_commit(queue: Queue, task_id: str) -> str:
    """The commit the task's last merge made, from its log; "" before it merged."""
    merges = [e for e in queue.read_log(task_id) if e["event"] == "merged" and e.get("commit")]
    return merges[-1]["commit"] if merges else ""


def advance_deployed(
    ctx: Context, staging_commit: str | None, prod_commit: str | None, includes: Callable[[str, str], bool | None]
) -> list[str]:
    """Keep each merged task's status in step with the builds staging and prod run: checked, on-staging (staging's
    build includes its merge) and promoted (prod's does), forward and back, as a push back takes a build away.
    ``includes(build, merge)`` answers None for a build git does not know: that build moves nothing."""
    queue = Queue.of(ctx)
    moved: list[str] = []

    def runs(build: str | None, merge: str) -> bool | None:
        return None if build is None else includes(build, merge)

    with ctx.lock, queue.lock():
        for task in queue.tasks()[0]:
            merge = merge_commit(queue, task.id) if task.status in ("checked", "on-staging", "promoted") else ""
            if not merge:
                continue
            on_staging, on_prod = runs(staging_commit, merge), runs(prod_commit, merge)
            while (step := _deploy_step(task.status, on_staging, on_prod)) is not None:
                to, event = step
                build = prod_commit if event in ("promoted", "left-prod") else staging_commit
                said = f"{'no longer ' if event.startswith('left') else ''}on {event.split('-')[-1]} at {build}"
                task = transition(ctx, queue, task, to, event, WARDEN, said)
                moved.append(f"{task.id} {to}")
    return moved


def _deploy_step(status: str, on_staging: bool | None, on_prod: bool | None) -> tuple[str, str] | None:
    """One move toward the status the builds say, or None once there; an unknown build (None) moves nothing. The
    moves exclude each other in pairs, so walking them ends."""
    if status == "promoted" and on_prod is False:
        return "on-staging", "left-prod"
    if status == "on-staging" and on_prod is True:
        return "promoted", "promoted"
    if status == "on-staging" and on_staging is False:
        return "checked", "left-staging"
    if status == "checked" and on_staging is True:
        return "on-staging", "on-staging"
    return None


def rescore(ctx: Context) -> int:
    """Score every waiting task again, as of now, as after a change to GOAL.md; how many scores changed."""
    queue = Queue.of(ctx)
    goals = goal_ranks(ctx)
    now = ctx.clock.now()
    with ctx.lock, queue.lock():
        changes = {}
        for task in queue.tasks()[0]:
            if task.status in SCORED:
                new = _scored_at(ctx, queue, task, goals, now)
                if new.score != task.score:
                    changes[task_rel(task.id)] = formats.dump_task(new)
        if changes:
            queue.write(changes)
        # Only once the scores are written: a rescore that failed is tried again at the next tick.
        store.write_json(_rescored_path(ctx.places), {"at": now})
    return len(changes)
