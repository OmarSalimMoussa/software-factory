"""YOLO mode: the factory runs without the owner, until the owner switches it off.

While it is on, the warden approves the orchestrator's proposals while fewer approved runs wait for a place than
`caps.implementers`, answers each question asked of the owner with ANSWER, as the owner, and hands each spec's Go and
each stuck task to the orchestrator to decide (`factory decide`). It never pushes: what waits for the owner's push to
staging keeps waiting. The daily budget, the guard and every other limit stay as they are.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from runtime.queue import cards, owner, proposals
from runtime.shared import store
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused, ok
from runtime.warden import inbox
from runtime.warden.context import Context

ANSWER = "Be proactive and solve it yourself."
# The choices each card the orchestrator decides offers it: the owner's own, but Check feedback, which is theirs alone.
CHOICES = {"spec": ("go",), "decide": ("extend", "designer", "park")}


def _path(places: Places) -> Path:
    return places.state / "yolo.json"


def state(places: Places) -> dict[str, Any] | None:
    """YOLO mode while it is on: since when, and the cards already handed to the orchestrator; None while off."""
    found: dict[str, Any] | None = store.read_json_or(_path(places), None)
    return found


def owner_switch(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """The owner switches YOLO mode on or off; the orchestrator hears of it at once."""
    on = args.get("on")
    if not isinstance(on, bool):
        raise Refused("args", "say whether YOLO mode is on: true or false")
    with ctx.lock:
        if (state(ctx.places) is not None) == on:
            return ok(on=on)
        if on:
            store.write_json(_path(ctx.places), {"since": ctx.clock.iso(), "told": []}, mode=0o600)
        else:
            _path(ctx.places).unlink()
    ctx.post_event(inbox.YOLO_ON if on else inbox.YOLO_OFF, {})
    return ok(on=on)


def run_pass(ctx: Context) -> None:
    if state(ctx.places) is None:
        return
    found = proposals.read(ctx.places)
    room = ctx.settings().get_int("caps.implementers") - len(found.approved)
    for task_id in [entry for entry in found.open if entry and entry != "none"][: max(room, 0)]:
        owner.approve(ctx, task_id, by="YOLO mode")
    open_cards = [c for c in cards.read(ctx.places) if not c.answered]
    for card in open_cards:
        if card.kind == "question":
            owner.answer(ctx, card.id, ANSWER)
    with ctx.lock:
        held = state(ctx.places)
        if held is None:
            return
        handed = [c for c in open_cards if c.kind in CHOICES and c.id not in held["told"]]
        if handed:
            held["told"] = [*held["told"], *(c.id for c in handed)]
            store.write_json(_path(ctx.places), held, mode=0o600)
    for card in handed:
        ctx.post_event(inbox.YOURS, {"card": card.id, "kind": card.kind, "task": card.task,
                                     "choices": list(CHOICES[card.kind])})  # fmt: skip


def orchestrator_decide(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """The orchestrator's decision on a card YOLO mode handed it, made as the owner would make it."""
    if state(ctx.places) is None:
        raise Refused("yolo", "only the owner decides while YOLO mode is off")
    card_id, choice = args.get("card"), args.get("choice")
    card = next((c for c in cards.read(ctx.places) if c.id == card_id and not c.answered), None)
    if card is None or card.kind not in CHOICES:
        raise Refused("card", f"no card {card_id} waits for a decision")
    if choice not in CHOICES[card.kind]:
        raise Refused("choice", f"{card.id} takes {' or '.join(CHOICES[card.kind])}")
    if choice == "go":
        return ok(**owner.go(ctx, card.id, by="the orchestrator"))
    return ok(**owner.decide(ctx, card.id, choice, by="the orchestrator"))
