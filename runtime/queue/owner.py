"""The owner's decisions from the dashboard: proposals, the approved order, boosts, parked work and cards.

None needs the code (an approval steers work and changes nothing in the factory). Each one posts an inbox event so
the orchestrator acts at once (R4). The warden writes the approval where no agent can (R20).
"""

from __future__ import annotations

from typing import Any

from runtime.queue import approvals, cards, documents, formats, intake, proposals, route, tasks
from runtime.queue.book import Queue, request_rel, task_rel
from runtime.queue.formats import dump_request, one_line
from runtime.queue.statuses import OWNER
from runtime.shared.models import RETIRED, STOPPED, Task
from runtime.shared.protocol import Refused
from runtime.warden import inbox
from runtime.warden.context import Context
from runtime.warden.sessions import lifecycle, limits, permits, registry


def _proposed(queue: Queue, task_id: str) -> Task:
    task = queue.task(task_id)
    if task.status != "proposed":
        raise Refused("stale", f"This changed; {task_id} is {task.status} now")
    return task


def approve(ctx: Context, task_id: str) -> dict[str, Any]:
    queue = Queue.of(ctx)
    with ctx.lock, queue.lock():
        task = _proposed(queue, task_id)
        if task.run is None:
            raise Refused("run", f"{task_id} has no planned run")
        model = route.model_for(ctx.settings(), task.run)
        routed = route.route(task, ctx.settings())
        approval = approvals.record(ctx.places, task, model, ctx.clock.iso(), routed)
        try:
            tasks.transition(ctx, queue, task, "approved", "approve", OWNER, f"approved at rung {task.run} on {model}")
        except BaseException:
            approvals.void(ctx.places, task_id)
            raise
    ctx.post_event(inbox.APPROVED, {"task": task_id, "rung": approval.rung, "model": model})
    return {"task": task_id, "rung": approval.rung, "model": model}


def later(ctx: Context, task_id: str) -> dict[str, Any]:
    queue = Queue.of(ctx)
    with ctx.lock, queue.lock():
        task = _proposed(queue, task_id)
        tasks.transition(ctx, queue, task, "queued", "not-now", OWNER, "not now", not_now=True)
    ctx.post_event(inbox.NOT_NOW, {"task": task_id})
    return {"task": task_id}


def drop(ctx: Context, task_id: str, reason: str) -> dict[str, Any]:
    if not reason.strip():
        raise Refused("reason", "a drop says why, so the task's requests can be answered")
    queue = Queue.of(ctx)
    with ctx.lock, queue.lock():
        task = queue.task(task_id)
        tasks.transition(ctx, queue, task, "closed", "drop", OWNER, f"dropped: {one_line(reason)}")
    ctx.post_event(inbox.DROPPED, {"task": task_id, "reason": one_line(reason), "requests": task.requests})
    return {"task": task_id}


def move(ctx: Context, task_id: str, direction: str, seen: object, approved: bool) -> dict[str, Any]:
    """Up or down among the proposals or the approved runs, only from the order the device saw (R29)."""
    with ctx.lock:
        found = proposals.read(ctx.places)
        if approved:
            proposals.reorder(found.approved, task_id, direction, seen)
        else:
            proposals.reorder(found.open, task_id, direction, seen)
        proposals.write(ctx.places, found)
    return {"task": task_id, "order": found.approved if approved else found.open}


def run(ctx: Context, task_id: str, rung: object) -> dict[str, Any]:
    """Change the run on a proposal before approving it; the approval records the rung chosen."""
    if isinstance(rung, bool) or not isinstance(rung, int) or rung not in range(1, 5):
        raise Refused("run", "a run is rung 1 to 4")
    queue = Queue.of(ctx)
    with ctx.lock, queue.lock():
        task = _proposed(queue, task_id)
        task.run = rung
        queue.write({task_rel(task_id): formats.dump_task(task)})
        queue.log(task_id, ctx.clock.iso(), OWNER, "owner-run", f"run changed by you to rung {rung}")
    return {"task": task_id, "run": rung}


def boost(ctx: Context, task_id: str, on: bool) -> dict[str, Any]:
    queue = Queue.of(ctx)
    with ctx.lock, queue.lock():
        task = queue.task(task_id)
        if task.status != "queued":
            raise Refused("boost", f"{task_id} is {task.status}; only a queued task is boosted")
        task.boosted_at = ctx.clock.iso() if on else ""
        if on:
            task.not_now = False
        queue.write({task_rel(task_id): formats.dump_task(task)})
        queue.log(task_id, ctx.clock.iso(), OWNER, "boost" if on else "unboost", "boosted" if on else "unboosted")
    if on:
        ctx.post_event(inbox.BOOSTED, {"task": task_id})
    return {"task": task_id, "boosted": on}


def boost_request(ctx: Context, request_id: str) -> dict[str, Any]:
    """A boosted request is triaged at once in a batch of its own, and its task arrives boosted: the warden's next
    tick names it as the next triage batch."""
    queue = Queue.of(ctx)
    with ctx.lock, queue.lock():
        req = queue.request(request_id)
        if req.outcome:
            raise Refused("boost", f"{request_id} already has its outcome: {req.outcome}")
        found = intake.boosts(ctx)
        found[request_id] = ctx.clock.iso()
        intake.set_boosts(ctx, found)
    return {"request": request_id}


def unpark(ctx: Context, item: str) -> dict[str, Any]:
    """A parked task goes back to the queue; a parked request waits for the next triage again."""
    queue = Queue.of(ctx)
    with ctx.lock, queue.lock():
        if item.startswith("t-"):
            tasks.transition(ctx, queue, queue.task(item), "queued", "unpark", OWNER, "unparked")
        else:
            req = queue.request(item)
            if not req.outcome.startswith("parked "):
                raise Refused("stale", f"This changed; {item} is not parked")
            req.outcome = ""
            queue.write({request_rel(req): dump_request(req)})
    return {"item": item}


def go(ctx: Context, card_id: str) -> dict[str, Any]:
    queue = Queue.of(ctx)
    with ctx.lock, queue.lock():
        found, card = cards.find(ctx.places, card_id, "spec")
        task = queue.task(card.task)
        approvals.record_go(ctx.places, task.id, ctx.clock.iso())
        tasks.transition(ctx, queue, task, "building", "go", OWNER, "Go on the spec")
        documents.approve_spec(ctx, queue, task.id)
        cards.settle(ctx, found, card, "go")
    ctx.post_event(inbox.GO, {"task": card.task, "card": card_id})
    return {"card": card_id, "task": card.task}


def feedback(ctx: Context, card_id: str) -> dict[str, Any]:
    """Check feedback hands the designer the owner's comments on the artifact; the warden delivers it, so nothing
    goes through the orchestrator."""
    _, card = cards.find(ctx.places, card_id, "spec")
    words = f"The owner left comments on the spec of {card.task} ({card.brief}); read and answer them."
    op = cards.wake(ctx, card, words, lambda: None)
    if op is None:
        raise Refused("feedback", "the designer who asked is not running now; its comments wait on the artifact")
    return {"card": card_id, "op": op}


def decide(ctx: Context, card_id: str, choice: str) -> dict[str, Any]:
    """A stuck task: Extend the spent limit of its live session, send it To a designer (rung 3), or Park it. The
    warden tells the session itself, or ends it when its task leaves the run, so nothing is relayed."""
    to, event, kind = {
        "extend": ("", "extend", inbox.DECIDED_EXTEND),
        "designer": ("proposed", "to-designer", inbox.TO_DESIGNER),
        "park": ("parked", "park", inbox.PARK),
    }[choice]
    queue = Queue.of(ctx)
    with ctx.lock, queue.lock():
        found, card = cards.find(ctx.places, card_id, "decide")
        task = queue.task(card.task)
        if choice == "extend":
            to = _before_decide(queue, task.id)
        sessions = lifecycle.read(ctx)
        live = next((s for s in sessions.values() if s.task == task.id and not s.parent and s.role in permits.CAP
                     and s.state not in (RETIRED,)), None)  # fmt: skip
        fields: dict[str, Any] = {"run": 3} if choice == "designer" else {}
        tasks.transition(ctx, queue, task, to, event, OWNER, f"{kind} by you", **fields)
        if choice == "extend" and live is not None:
            extra = limits.raise_limit(ctx, sessions, live, card.limit, by_orchestrator=False)
            registry.write(ctx.places, sessions)
        cards.settle(ctx, found, card, kind)
    op = None
    if live is not None and choice == "extend":
        again = " Your merge turn ended while you waited: it comes back with the next turn." if to == "qa-clean" else ""
        words = f"The owner extended your {card.limit} to {extra} more than the setting: carry on.{again}"
        op = cards.tell(ctx, live, words, lambda: None)
    elif live is not None:
        lifecycle.retire_session(ctx, live.id, STOPPED, lifecycle.OWNER)
    ctx.post_event(kind, {"task": card.task, "card": card_id})
    return {"card": card_id, "task": card.task, "status": to, "op": op}


def _before_decide(queue: Queue, task_id: str) -> str:
    """The running status a spent limit took the task from: a designer's task goes back to designing, an
    implementer's to where it was, so its own session carries on. A merge turn is not held while the owner
    decides: the task waits for its next one, qa-clean."""
    moves = [e for e in queue.read_log(task_id) if e.get("to") == "decide" and e.get("from")]
    if not moves:
        raise Refused("decide", f"{task_id}'s log says no status it came to the decide card from")
    return "qa-clean" if moves[-1]["from"] == "turn" else str(moves[-1]["from"])


def answer(ctx: Context, card_id: str, text: str) -> dict[str, Any]:
    if not text.strip():
        raise Refused("answer", "the answer is empty")
    with ctx.lock:
        found, card = cards.find(ctx.places, card_id, "question")
        cards.settle(ctx, found, card, text)
    words = f"The owner answered your question on {card.task or 'your work'}: {one_line(text)}"

    def to_orchestrator() -> None:
        """The orchestrator asked, or the answer did not reach the asker: the orchestrator hears it and acts on it."""
        ctx.post_event(inbox.ANSWERED, {"card": card_id, "task": card.task, "session": card.asked_by,
                                        "answer": one_line(text)})  # fmt: skip

    op = cards.wake(ctx, card, words, to_orchestrator)
    if op is None:
        to_orchestrator()
    return {"card": card_id, "op": op}
