"""Cards that ask for the owner beside the proposals: a spec's second sign-off, a stuck task's decision, and a
session's question (routing spec, R14, R15). Each carries a picture of what it asks; the warden keeps them in its
own cards.json, where no agent writes.
"""

from __future__ import annotations

import dataclasses

from runtime.queue import formats, pictures, records, statuses, tasks
from runtime.queue.book import Queue, spec_rel
from runtime.shared import models, store
from runtime.shared.models import WAITING, WORKING, Card
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused
from runtime.warden import lifecycle, registry
from runtime.warden.context import Context


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


def needs_owner(ctx: Context, task_id: str, ask: str, brief: str, question: str, picture: bytes | None) -> Card:
    """A spec's sign-off (with its artifact link) or a question (with a text answer); both carry a picture."""
    if picture is None:
        raise Refused("picture", "a card for the owner carries a picture: --picture <file.svg|file.png>")
    if bool(brief) == bool(question):
        raise Refused("needs-owner", "ask for a spec's sign-off with --brief <artifact> --ask, or ask --question")
    queue = Queue.of(ctx)
    by = tasks.actor(ctx)
    with ctx.lock, queue.lock():
        task = queue.task(task_id) if task_id or brief else None
        now = ctx.clock.iso()
        if question:
            title = task.title if task else "A question"
            card = Card("", "question", task_id, title, formats.one_line(question), now, asked_by=ctx.caller.session)
            return _waiting(ctx, _add(ctx, card, picture))
        assert task is not None
        if not brief.startswith(formats.ARTIFACT):
            raise Refused("brief", f"the brief is a {formats.ARTIFACT} artifact link")
        if not ask.strip():
            raise Refused("needs-owner", "say in one line what the owner signs off: --ask '…'")
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


def wake(ctx: Context, card: Card, message: str) -> None:
    """Resume the session that asked, with the owner's word."""
    asker = registry.read(ctx.places).get(card.asked_by)
    if asker is not None and asker.state == WAITING:
        lifecycle.wake(ctx, asker, message)


def to_decide(ctx: Context, task_id: str, why: str) -> Card:
    """A running task whose rounds or check runs are spent goes to the owner: Extend, To a designer or Park."""
    queue = Queue.of(ctx)
    with ctx.lock, queue.lock():
        task = tasks.transition(ctx, queue, queue.task(task_id), "decide", "limits", statuses.WARDEN, why)
        return _add(ctx, Card("", "decide", task_id, task.title, formats.one_line(why), ctx.clock.iso()), None)


def comments(ctx: Context, task_id: str, count: object) -> Card:
    """The designer's latest count of the owner's comments on the spec's artifact (R14)."""
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise Refused("comments", "the comment count is a whole number from 0")
    with ctx.lock:
        cards = read(ctx.places)
        card = next((c for c in reversed(cards) if c.task == task_id and c.kind == "spec" and not c.answered), None)
        if card is None:
            raise Refused("card", f"{task_id} has no spec waiting for the owner")
        card.comments, card.comments_at = count, ctx.clock.iso()
        write(ctx.places, cards)
    return card


def settle(ctx: Context, cards: list[Card], card: Card, answer: str) -> None:
    card.answered = formats.one_line(answer)
    write(ctx.places, cards)
