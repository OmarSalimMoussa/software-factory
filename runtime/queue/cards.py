"""Cards that ask for the owner beside the proposals: a spec's second sign-off, a stuck task's decision, and a
session's question (routing spec, R14, R15). Each carries a picture of what it asks; the warden keeps them in its
own cards.json, where no agent writes.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable

from runtime.queue import formats, pictures, records, statuses, tasks
from runtime.queue.book import Queue, spec_rel
from runtime.shared import models, operations, store
from runtime.shared.models import WAITING, WORKING, Card, Session, Task
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused
from runtime.warden.context import Context
from runtime.warden.owner import briefs, ops
from runtime.warden.sessions import lifecycle, limits, registry
from runtime.warden.sessions.launch import LaunchError

log = logging.getLogger(__name__)


def read(places: Places) -> list[Card]:
    raw = store.read_json_or(places.cards, [])
    if not isinstance(raw, list):
        raise store.StoreError(f"{places.cards}: not a list of cards")
    return [models.load(Card, row) for row in raw]


def write(places: Places, cards: list[Card]) -> None:
    store.write_json(places.cards, [models.dump(c) for c in cards], mode=0o600)


def find(places: Places, card_id: str, kind: str) -> tuple[list[Card], Card]:
    cards = read(places)
    card = next((c for c in cards if c.id == card_id), None)
    if card is None or card.kind != kind:
        raise Refused("card", f"no {kind} card {card_id}")
    if card.answered:
        raise Refused("card", f"{card_id} was already answered: {card.answered}")
    return cards, card


def _add(ctx: Context, card: Card, picture: bytes | None) -> Card:
    cards = read(ctx.places)
    card.id = f"n-{len(cards) + 1:04d}"
    if picture is not None:
        data, kind = pictures.clean(picture, ctx.settings().get_int("queue.max_upload_kib"))
        card.picture = pictures.save_card_picture(ctx.places, card.id, data, kind)
    write(ctx.places, [*cards, card])
    return card


def needs_owner(ctx: Context, task_id: str, ask: str, question: str, picture: bytes | None) -> Card:
    """A spec's sign-off, on the task's latest saved brief, or a question with a text answer; both carry a picture."""
    if picture is None:
        raise Refused("picture", "a card for the owner carries a picture: --picture <file.svg|file.png>")
    if bool(ask) == bool(question):
        raise Refused("needs-owner", "ask for a spec's sign-off with <task> --ask, or ask --question")
    queue = Queue.of(ctx)
    by = tasks.actor(ctx)
    with ctx.lock, queue.lock():
        task = queue.task(task_id) if task_id or ask else None
        now = ctx.clock.iso()
        if question:
            title = task.title if task else "A question"
            card = Card("", "question", task_id, title, formats.one_line(question), now, asked_by=ctx.caller.session)
            return _waiting(ctx, _add(ctx, card, picture))
        assert task is not None
        saved = briefs.read(ctx.places, task_id)["versions"]
        if not saved:
            raise Refused("brief", f"save the brief of {task_id} first: factory brief save {task_id} <file.html>")
        brief = f"v{len(saved)}"
        spec = queue.spec(task_id)
        if spec is None:
            raise Refused("spec", f"save the spec of {task_id} first: factory spec save {task_id} <file>")
        queue.write(
            {spec_rel(task_id): records.dump_spec(dataclasses.replace(spec, status="review", brief=brief, updated=now))}
        )
        tasks.transition(ctx, queue, task, "review", "needs-owner", by, f"spec for the owner: {brief}")
        card = Card(
            "", "spec", task_id, task.title, formats.one_line(ask), now, brief=brief, asked_by=ctx.caller.session
        )
        return _waiting(ctx, _add(ctx, card, picture))


def _waiting(ctx: Context, card: Card) -> Card:
    """The session that asked waits for the owner, so it holds its place without working. The orchestrator never
    waits: the owner's answer reaches it through its inbox."""
    with ctx.lock:
        sessions = registry.read(ctx.places)
        asker = sessions.get(card.asked_by)
        if asker is not None and asker.role != statuses.ORCH and asker.state == WORKING:
            lifecycle.move(ctx, asker, WAITING, statuses.WARDEN, f"waits for the owner on {card.id}")
            registry.write(ctx.places, sessions)
    return card


def tell(ctx: Context, s: Session, message: str, missed: Callable[[], None]) -> str:
    """Hand a session the owner's word, and it works again: the operation the page follows, since handing it over
    holds the session longer than the dashboard waits for an answer. ``missed`` runs when the word does not arrive."""

    def hand() -> str:
        try:
            lifecycle.deliver(ctx, s.id, message, statuses.OWNER)
        except Refused:
            missed()
            raise
        except LaunchError as exc:
            missed()
            raise ops.StepError(f"it did not reach {s.name}: {exc}") from exc
        return "told"

    return ops.launch(ctx, operations.WORD, [(f"Tell {s.name}", hand)])


def wake(ctx: Context, card: Card, message: str, missed: Callable[[], None]) -> str | None:
    """Tell the session that asked the owner's word; None when no session can hear it. The orchestrator hears
    through its inbox instead, and a session no longer working or waiting has nothing to hear: the card keeps the
    answer either way."""
    sessions = registry.read(ctx.places)
    sid = registry.following(ctx.places, sessions, card.asked_by)
    asker = sessions[sid] if sid is not None else None
    if asker is None or asker.role == statuses.ORCH or asker.state not in (WORKING, WAITING):
        return None
    return tell(ctx, asker, message, missed)


def to_decide(ctx: Context, task_id: str, why: str, limit: str) -> Card:
    """A running task whose rounds or check runs are spent goes to the owner: Extend, To a designer or Park."""
    queue = Queue.of(ctx)
    with ctx.lock, queue.lock():
        task = tasks.transition(ctx, queue, queue.task(task_id), "decide", "limits", statuses.WARDEN, why)
        card = Card("", "decide", task_id, task.title, formats.one_line(why), ctx.clock.iso(), limit=limit)
        return _add(ctx, card, None)


def spent(ctx: Context, s: Session, limit: str, why: str) -> bool:
    """``s`` used up ``limit`` again after the orchestrator's one extension: its running task goes to the owner's
    decide card, once. Whether the task now waits for the owner."""
    if not s.task or s.parent or not limits.extended_once(s, limit):
        return False
    with ctx.lock:
        task = Queue.of(ctx).task(s.task)
        if task.status == "decide":
            return True
        if task.status not in statuses.RUNNING:
            return False
        to_decide(ctx, s.task, why, limit)
    return True


def blocked(ctx: Context, task: Task, why: str) -> Card:
    return _add(ctx, Card("", "blocked", task.id, task.title, formats.one_line(why), ctx.clock.iso()), None)


def unblocked(ctx: Context, task_id: str, status: str) -> None:
    """The task left blocked, by the owner's Fixed or any other move: its card is answered with where it went."""
    found = read(ctx.places)
    open_cards = [c for c in found if c.kind == "blocked" and c.task == task_id and not c.answered]
    for card in open_cards:
        card.answered = status
    if open_cards:
        write(ctx.places, found)


def settle(ctx: Context, cards: list[Card], card: Card, answer: str) -> None:
    card.answered = formats.one_line(answer)
    write(ctx.places, cards)
