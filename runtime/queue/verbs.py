"""The queue's warden verbs (runtime/warden/verbs.py names each one). A handler reads its arguments, calls one
queue function and returns its answer; the queue functions refuse what breaks a rule.
"""

from __future__ import annotations

from typing import Any

from runtime.queue import cards, documents, formats, intake, owner, tasks, triage, turn
from runtime.queue import propose as proposing
from runtime.queue import route as ladders
from runtime.queue.book import Queue
from runtime.shared.models import REQUEST_SEVERITIES
from runtime.shared.protocol import Refused, ok
from runtime.warden import inbox, registry
from runtime.warden.context import Context

Args = dict[str, Any]
CAUSE_TRY = "cause-try"


def _text(args: Args, key: str, required: bool = True) -> str:
    value = args.get(key, "")
    if not isinstance(value, str) or (required and not value.strip()):
        raise Refused("args", f"{key} is required" if required else f"{key} is text")
    return value


def cause_try(ctx: Context, args: Args, data: bytes | None) -> dict[str, Any]:
    """An implementer's try at confirming a cause that did not hold, with what ruled it out, in its task's log. The
    warden counts them: at routing.cause_tries in one session it tells the orchestrator the task needs a higher rung,
    and the session tries no more."""
    session = registry.read(ctx.places).get(ctx.caller.session)
    if session is None or session.role != "implementer" or not session.task:
        raise Refused("cause", "a cause is tried in an implementer's run")
    tried = formats.one_line(_text(args, "tried"))
    limit = ctx.settings().get_int("routing.cause_tries")
    queue = Queue.of(ctx)
    with ctx.lock, queue.lock():
        earlier = [e for e in queue.read_log(session.task) if e.get("event") == CAUSE_TRY
                   and e.get("actor") == session.name and e["at"] >= session.started]  # fmt: skip
        if len(earlier) >= limit:
            raise Refused("cause-tries", f"your {limit} tries are spent: stop, and wait for the orchestrator")
        queue.log(session.task, ctx.clock.iso(), session.name, CAUSE_TRY, tried)
    left = limit - len(earlier) - 1
    if left:
        return ok(tries_left=left)
    why = f"{limit} tries did not confirm a cause"
    ctx.post_event(inbox.NEEDS_RUNG, {"session": session.id, "name": session.name, "task": session.task, "why": why})
    return ok(tries_left=0, message="That was your last try: stop, and report what you know and what you ruled out. "
                                    "The warden told the orchestrator, which moves the task up a rung.")  # fmt: skip


def request_new(ctx: Context, args: Args, data: bytes | None) -> dict[str, Any]:
    """An agent's request goes to the agents inbox; the owner's words, quoted, to the owner inbox."""
    severity = _text(args, "severity")
    if severity not in REQUEST_SEVERITIES:
        raise Refused("args", f"severity is one of {', '.join(REQUEST_SEVERITIES)}")
    quoting = _text(args, "from", required=False) == "owner" or tasks.actor(ctx) == "owner"
    if quoting and tasks.actor(ctx) not in ("owner", "orchestrator"):
        raise Refused("owner-inbox", "only the orchestrator files the owner's own words")
    body = intake.body_text(data, "request", ctx.settings().get_int("queue.max_upload_kib"))
    queue = Queue.of(ctx)
    with ctx.lock, queue.lock():
        inbox, sender = ("owner", "owner") if quoting else ("agents", tasks.actor(ctx))
        req = intake.file_request(ctx, queue, inbox, sender, severity, _text(args, "where"), body)
    return ok(request=req.id, inbox=req.inbox)


def triage_apply(ctx: Context, args: Args, data: bytes | None) -> dict[str, Any]:
    return ok(**triage.apply(ctx, data))


def triage_plan(ctx: Context, args: Args, data: bytes | None) -> dict[str, Any]:
    return ok(**triage.save_plan(ctx, data))


def task_new(ctx: Context, args: Args, data: bytes | None) -> dict[str, Any]:
    body = intake.body_text(data, "task body", ctx.settings().get_int("queue.max_upload_kib"))
    task = tasks.new_task(ctx, args, body)
    return ok(task=task.id, score=task.score)


def task_facts(ctx: Context, args: Args, data: bytes | None) -> dict[str, Any]:
    task = tasks.set_facts(ctx, _text(args, "task"), {k: v for k, v in args.items() if k != "task"})
    return ok(task=task.id, status=task.status, score=task.score)


def task_status(ctx: Context, args: Args, data: bytes | None) -> dict[str, Any]:
    task = tasks.set_status(
        ctx,
        _text(args, "task"),
        _text(args, "to"),
        _text(args, "event", required=False),
        _text(args, "note", required=False),
    )
    return ok(task=task.id, status=task.status)


def queue_next(ctx: Context, args: Args, data: bytes | None) -> dict[str, Any]:
    return ok(**tasks.next_up(ctx))


def route(ctx: Context, args: Args, data: bytes | None) -> dict[str, Any]:
    task = tasks.read(ctx, _text(args, "task"))
    ladder = ladders.ladder(task, ctx.settings())
    rung = next(r.n for r in ladder if r.holds)
    rows = [{"rung": r.n, "word": r.word, "holds": r.holds, "unmet": list(r.unmet)} for r in ladder]
    model = ladders.model_for(ctx.settings(), rung) if rung else None
    return ok(task=task.id, rung=rung, word=ladders.WORDS[rung], model=model, ladder=rows)


def propose(ctx: Context, args: Args, data: bytes | None) -> dict[str, Any]:
    return ok(
        **proposing.propose(
            ctx,
            _text(args, "task"),
            args.get("run"),
            _text(args, "why", required=False),
            _text(args, "reason", required=False),
            data,
            _text(args, "brief", required=False),
        )
    )


def propose_none(ctx: Context, args: Args, data: bytes | None) -> dict[str, Any]:
    return ok(**proposing.propose_none(ctx))


def spec_save(ctx: Context, args: Args, data: bytes | None) -> dict[str, Any]:
    spec = documents.save_spec(ctx, _text(args, "task"), data, args.get("slices"))
    return ok(task=spec.task, status=spec.status)


def card_comments(ctx: Context, args: Args, data: bytes | None) -> dict[str, Any]:
    card = cards.comments(ctx, _text(args, "task"), args.get("count"))
    return ok(card=card.id, comments=card.comments)


def needs_owner(ctx: Context, args: Args, data: bytes | None) -> dict[str, Any]:
    optional = {k: _text(args, k, required=False) for k in ("task", "ask", "brief", "question")}
    card = cards.needs_owner(ctx, optional["task"], optional["ask"], optional["brief"], optional["question"], data)
    return ok(card=card.id, kind=card.kind)


def check_save(ctx: Context, args: Args, data: bytes | None) -> dict[str, Any]:
    """A check of the default branch; the task its merge landed is checked when nothing failed."""
    record = documents.save_check(ctx, _text(args, "commit"), data)
    failed = [r.scenario for r in record.rows if r.result != "pass"]
    checked = turn.check_saved(ctx, record.commit, not failed)
    return ok(check=record.id, scenarios=len(record.rows), not_passed=failed, checked=checked or None)


# The owner's decisions, from the dashboard over owner.sock.


def owner_approve(ctx: Context, args: Args, data: bytes | None) -> dict[str, Any]:
    return ok(**owner.approve(ctx, _text(args, "task")))


def owner_later(ctx: Context, args: Args, data: bytes | None) -> dict[str, Any]:
    return ok(**owner.later(ctx, _text(args, "task")))


def owner_drop(ctx: Context, args: Args, data: bytes | None) -> dict[str, Any]:
    return ok(**owner.drop(ctx, _text(args, "task"), _text(args, "reason")))


def owner_move(ctx: Context, args: Args, data: bytes | None) -> dict[str, Any]:
    return ok(**owner.move(ctx, _text(args, "task"), _text(args, "direction"), args.get("seen"), approved=False))


def owner_run(ctx: Context, args: Args, data: bytes | None) -> dict[str, Any]:
    return ok(**owner.run(ctx, _text(args, "task"), args.get("run")))


def owner_approved_move(ctx: Context, args: Args, data: bytes | None) -> dict[str, Any]:
    return ok(**owner.move(ctx, _text(args, "task"), _text(args, "direction"), args.get("seen"), approved=True))


def owner_boost(ctx: Context, args: Args, data: bytes | None) -> dict[str, Any]:
    return ok(**owner.boost(ctx, _text(args, "task"), on=True))


def owner_unboost(ctx: Context, args: Args, data: bytes | None) -> dict[str, Any]:
    return ok(**owner.boost(ctx, _text(args, "task"), on=False))


def owner_request_boost(ctx: Context, args: Args, data: bytes | None) -> dict[str, Any]:
    return ok(**owner.boost_request(ctx, _text(args, "request")))


def owner_unpark(ctx: Context, args: Args, data: bytes | None) -> dict[str, Any]:
    return ok(**owner.unpark(ctx, _text(args, "item")))


def owner_go(ctx: Context, args: Args, data: bytes | None) -> dict[str, Any]:
    return ok(**owner.go(ctx, _text(args, "card")))


def owner_feedback(ctx: Context, args: Args, data: bytes | None) -> dict[str, Any]:
    return ok(**owner.feedback(ctx, _text(args, "card")))


def owner_extend(ctx: Context, args: Args, data: bytes | None) -> dict[str, Any]:
    return ok(**owner.decide(ctx, _text(args, "card"), "extend"))


def owner_to_designer(ctx: Context, args: Args, data: bytes | None) -> dict[str, Any]:
    return ok(**owner.decide(ctx, _text(args, "card"), "designer"))


def owner_park(ctx: Context, args: Args, data: bytes | None) -> dict[str, Any]:
    return ok(**owner.decide(ctx, _text(args, "card"), "park"))


def owner_answer(ctx: Context, args: Args, data: bytes | None) -> dict[str, Any]:
    return ok(**owner.answer(ctx, _text(args, "card"), _text(args, "answer")))
