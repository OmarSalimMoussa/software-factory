"""The batch lane (intake spec): the warden names each batch, triage proposes, code applies.

``batch`` names the next batch, the one rule both the warden's `triage due` and the dashboard read: the waiting
requests (no outcome yet, and not severe: those take the fast lane), oldest first, at most intake.batch_size. A
request the owner boosted goes at once, in a batch of its own. The triage subagent saves its plan with `factory
triage plan`; `factory triage apply` applies the saved plan. Every request in the plan's batch gets exactly one
outcome: a new task, joining an open task, noise or parked. Every problem is found before anything is written, and
then every file is written at once, or nothing is.

    {"batch": ["r-…"],
     "new_tasks": [{"title", "body", "goal_item", "severity", "surfaces", "cause", "slices", "owner_class",
                    "requests", "after"}],
     "joins": {"r-…": "t-…"}, "noise": {"r-…": "why"}, "parked": {"r-…": "why"}}
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping
from typing import Any

from runtime.queue import formats, intake, statuses, tasks
from runtime.queue.book import Queue, request_rel, task_rel
from runtime.queue.formats import FormatError
from runtime.queue.score import score
from runtime.shared import store
from runtime.shared.models import Request, Task
from runtime.shared.protocol import Refused
from runtime.warden.context import Context

PLAN_KEYS = frozenset({"batch", "new_tasks", "joins", "noise", "parked"})
PLAN_FILE = "triage-plan.json"


@dataclasses.dataclass(frozen=True, slots=True)
class Batch:
    ids: list[str]  # the requests the next triage pass takes, oldest first
    due: bool  # whether that pass is due now
    boosted: bool  # a request the owner boosted, alone
    waiting: int  # every request waiting for triage
    oldest_at: str | None  # when the oldest waiting request arrived; None when it cannot be read or none waits
    due_at: str | None  # when the pass is due: now when it is, else when the oldest has waited long enough


def batch(ctx: Context) -> Batch:
    """The next triage batch, and whether it is due: a boosted request at once, else when intake.batch_size
    requests wait or the oldest has waited intake.batch_max_wait_hours. A request that cannot be read still waits,
    with no known age; the queue-format sweep reports it."""
    settings = ctx.settings()
    good, broken = Queue.of(ctx).requests()
    waiting = sorted((r for r in good if not r.outcome and r.severity != "severe"), key=lambda r: (r.at, r.id))
    boosted = sorted((at, rid) for rid, at in intake.boosts(ctx).items() if rid in {r.id for r in waiting})
    count = len(waiting) + len(broken)
    now = ctx.clock.iso()
    if boosted:
        return Batch([boosted[0][1]], True, True, count, waiting[0].at if waiting else None, now)
    size = settings.get_int("intake.batch_size")
    oldest = waiting[0].at if waiting else None
    aged_at = formats.epoch(oldest) + settings.get_int("intake.batch_max_wait_hours") * 3600 if oldest else None
    due = bool(waiting) and (count >= size or (aged_at is not None and ctx.clock.now() >= aged_at))
    due_at = now if due else formats.iso(aged_at) if aged_at is not None else None
    return Batch([r.id for r in waiting[:size]], due, False, count, oldest, due_at)


def _plan_path(ctx: Context) -> Any:
    return ctx.places.state / PLAN_FILE


def save_plan(ctx: Context, data: bytes | None) -> dict[str, Any]:
    """The triage subagent's plan, kept for the orchestrator's `factory triage apply`; its shape is checked now."""
    plan = _plan(data, ctx.settings().get_int("queue.max_upload_kib"))
    store.write_json(_plan_path(ctx), plan, mode=0o600)
    return {"batch": len(plan["batch"]) if isinstance(plan["batch"], list) else 0, "saved": True}


NEW_TASK_KEYS = frozenset(
    {"title", "body", "goal_item", "severity", "surfaces", "cause", "slices", "owner_class", "requests", "after"}
)


def _plan(data: bytes | None, limit_kib: int) -> dict[str, Any]:
    text = intake.body_text(data, "triage plan", limit_kib)
    try:
        plan = json.loads(text)
    except ValueError as exc:
        raise Refused("triage-plan", f"the plan is not JSON ({exc})") from exc
    if not isinstance(plan, dict) or set(plan) != PLAN_KEYS:
        raise Refused("triage-plan", f"the plan holds exactly {', '.join(sorted(PLAN_KEYS))}")
    return plan


def _ids(value: object, what: str, problems: list[str]) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        problems.append(f"{what} is a list of ids")
        return []
    return list(value)


def _reasons(value: object, what: str, problems: list[str]) -> dict[str, str]:
    if not isinstance(value, dict) or not all(isinstance(v, str) and v.strip() for v in value.values()):
        problems.append(f"{what} maps each request to a one-line reason or task")
        return {}
    return {k: formats.one_line(v) for k, v in value.items()}


def apply(ctx: Context, data: bytes | None) -> dict[str, Any]:
    """Apply the plan in ``data``, or the one triage saved when none is given."""
    settings = ctx.settings()
    if data is None:
        try:
            data = _plan_path(ctx).read_bytes()
        except FileNotFoundError as exc:
            raise Refused("triage-plan", "triage saved no plan: it runs factory triage plan --body <file>") from exc
    plan = _plan(data, settings.get_int("queue.max_upload_kib"))
    goals = tasks.goal_ranks(ctx)
    queue = Queue.of(ctx)
    with ctx.lock, queue.lock():
        problems: list[str] = []
        batch = _ids(plan["batch"], "batch", problems)
        joins = _reasons(plan["joins"], "joins", problems)
        noise = _reasons(plan["noise"], "noise", problems)
        parked = _reasons(plan["parked"], "parked", problems)
        new_specs = plan["new_tasks"] if isinstance(plan["new_tasks"], list) else []
        if not isinstance(plan["new_tasks"], list):
            problems.append("new_tasks is a list")
        requests = _batch(queue, batch, problems)
        existing = {t.id: t for t in queue.tasks()[0]}
        outcomes = _outcomes(batch, new_specs, joins, noise, parked, problems)
        for rid, target in joins.items():
            if target not in existing or existing[target].status == "closed":
                problems.append(f"{rid} joins {target}, which is not an open task")
        now = ctx.clock.iso()
        boosted = intake.boosts(ctx)
        made = [_new_task(queue, n, spec, goals, existing, now, problems) for n, spec in enumerate(new_specs)]
        if problems:
            raise Refused("triage-plan", " | ".join(problems))
        changes, made_tasks = _changes(
            ctx, queue, made, requests, outcomes, joins, noise, parked, existing, boosted, goals, now
        )
        queue.write(changes)
        for task in made_tasks:
            queue.log(task.id, now, statuses.ORCH, "triage", f"made from {', '.join(task.requests)}", to="queued")
        for rid, target in joins.items():
            queue.log(target, now, statuses.ORCH, "triage", f"joined by {rid}")
        intake.set_boosts(ctx, {k: v for k, v in boosted.items() if k not in batch})
        summary = {
            "at": now,
            "batch": len(batch),
            "new": [t.id for t in made_tasks],
            "joined": len(joins),
            "noise": len(noise),
            "parked": len(parked),
        }
        store.write_json(ctx.places.state / "triage-last.json", summary, mode=0o600)
        _plan_path(ctx).unlink(missing_ok=True)
    set_aside = [
        {"request": r, "outcome": requests[r].outcome} for r in (*noise, *parked) if requests[r].inbox == "owner"
    ]
    return {**summary, "owner_requests_set_aside": set_aside}


def _batch(queue: Queue, batch: list[str], problems: list[str]) -> dict[str, Request]:
    found: dict[str, Request] = {}
    if len(set(batch)) != len(batch):
        problems.append("the batch names a request twice")
    for rid in batch:
        try:
            req = queue.request(rid)
        except FileNotFoundError:
            problems.append(f"{rid} does not exist")
            continue
        if req.outcome:
            problems.append(f"{rid} already has its outcome: {req.outcome}")
        found[rid] = req
    return found


def _outcomes(
    batch: list[str],
    new_specs: list[Any],
    joins: Mapping[str, str],
    noise: Mapping[str, str],
    parked: Mapping[str, str],
    problems: list[str],
) -> dict[str, list[str]]:
    """Every outcome each request was given; each batch request must have exactly one."""
    given: dict[str, list[str]] = {}
    for n, spec in enumerate(new_specs):
        linked = spec.get("requests") if isinstance(spec, dict) else None
        for rid in linked if isinstance(linked, list) else []:
            given.setdefault(str(rid), []).append(f"new task {n + 1}")
    for kind, mapping in (("joins", joins), ("noise", noise), ("parked", parked)):
        for rid in mapping:
            given.setdefault(rid, []).append(kind)
    for rid in batch:
        if len(given.get(rid, [])) != 1:
            problems.append(f"{rid} has {len(given.get(rid, []))} outcomes; it needs exactly one")
    problems += [f"{rid} is not in the batch" for rid in given if rid not in batch]
    return given


def _new_task(
    queue: Queue,
    n: int,
    spec: object,
    goals: Mapping[str, int],
    existing: Mapping[str, Task],
    now: str,
    problems: list[str],
) -> Task | None:
    where = f"new task {n + 1}"
    if not isinstance(spec, dict) or set(spec) != NEW_TASK_KEYS:
        problems.append(f"{where} holds exactly {', '.join(sorted(NEW_TASK_KEYS))}")
        return None
    try:
        facts = tasks.facts_from({k: v for k, v in spec.items() if k not in ("body", "requests")})
    except Refused as exc:
        problems.append(f"{where}: {exc.reason}")
        return None
    if not isinstance(spec["body"], str) or not isinstance(spec["requests"], list) or not spec["requests"]:
        problems.append(f"{where} has a body and links its requests")
        return None
    task = Task(
        id="",
        title="",
        requests=[str(r) for r in spec["requests"]],
        goal_item="",
        severity="",
        surfaces=[],
        status=statuses.NEW,
        created=now,
        body=spec["body"].strip(),
    )
    task = dataclasses.replace(task, **facts)
    if task.goal_item and task.goal_item not in goals:
        problems.append(f"{where} names {task.goal_item}, which is no open item of GOAL.md")
    problems += [f"{where} waits for {a}, which does not exist" for a in task.after if a not in existing]
    try:
        formats.parse_task(formats.dump_task(dataclasses.replace(task, id="t-0000", status="queued")))
    except FormatError as exc:
        problems += [f"{where}: {p}" for p in exc.problems]
    return task


def _changes(
    ctx: Context,
    queue: Queue,
    made: list[Task | None],
    requests: Mapping[str, Request],
    outcomes: Mapping[str, list[str]],
    joins: Mapping[str, str],
    noise: Mapping[str, str],
    parked: Mapping[str, str],
    existing: Mapping[str, Task],
    boosted: Mapping[str, str],
    goals: Mapping[str, int],
    now: str,
) -> tuple[dict[str, str], list[Task]]:
    changes: dict[str, str] = {}
    first = int(queue.next_task_id()[2:])
    made_tasks: list[Task] = []
    for n, draft in enumerate(t for t in made if t is not None):
        task = dataclasses.replace(statuses.move(draft, "queued", "triage", statuses.ORCH), id=f"t-{first + n:04d}")
        if any(r in boosted for r in task.requests):
            task.boosted_at = now
        for rid in task.requests:
            requests[rid].outcome = f"task {task.id}"
        made_tasks.append(task)
    joined: dict[str, Task] = {}
    for rid, target in joins.items():
        task = joined.setdefault(target, dataclasses.replace(existing[target]))
        task.requests = [*task.requests, rid]
        task.not_now = False
        if rid in boosted:
            task.boosted_at = now
        requests[rid].outcome = f"joined {target}"
    for rid, why in noise.items():
        requests[rid].outcome = f"noise {why}"
    for rid, why in parked.items():
        requests[rid].outcome = f"parked {why}"
    for rid in outcomes:
        changes[request_rel(requests[rid])] = formats.dump_request(requests[rid])
    for task in [*made_tasks, *joined.values()]:
        if task.status in tasks.SCORED:
            reqs = [requests[r] if r in requests else queue.request(r) for r in task.requests]
            task.score = score(task, reqs, goals, ctx.clock.now(), ctx.settings())
        changes[task_rel(task.id)] = formats.dump_task(task)
    return changes, made_tasks
