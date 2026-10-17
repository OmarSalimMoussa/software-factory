"""YOLO mode: the factory runs without the owner, until the owner switches it off.

While it is on, the warden approves the orchestrator's proposals in the seat order, each only into a free place of
its role: the role's cap, less the runs that hold a place, the benched ones and the runs approved or designed and
waiting. So YOLO never starts a run the next one benches. It answers each question asked of the owner with ANSWER,
as the owner, and hands each spec's Go and each stuck task to the orchestrator to decide (`factory decide`). It never
pushes: what waits for the owner's push to prod keeps waiting. The daily budget, the guard and every other limit
stay as they are.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from runtime.queue import cards, owner, proposals
from runtime.shared import order, store
from runtime.shared.models import DEPRIORITIZED, RETIRED
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused, ok
from runtime.warden import inbox
from runtime.warden.context import Context
from runtime.warden.sessions import permits, registry
from runtime.warden.sessions.permits import IMPL

ANSWER = "Be proactive and solve it yourself."
# The choices each card the orchestrator decides offers it: the owner's own, but Send feedback, which is theirs alone.
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


def _free(ctx: Context, role: str) -> int:
    """The places of ``role`` that no run holds, was benched from or waits for."""
    sessions = registry.read(ctx.places).values()
    held = len(registry.seated(sessions, role))
    benched = [s for s in sessions if s.role == role and s.state == DEPRIORITIZED and not s.parent]
    held += sum(1 for s in benched if permits.has_work(ctx, s))
    live = {s.task for s in sessions if s.role == role and s.state != RETIRED and not s.parent}
    waiting = list(proposals.read(ctx.places).approved) + (
        proposals.designed_builds(ctx.places) if role == IMPL else []
    )
    held += sum(1 for t in dict.fromkeys(waiting) if t not in live and permits.needed_role(ctx, t) == role)
    return ctx.settings().get_int(permits.CAP[role]) - held


def run_pass(ctx: Context) -> None:
    if state(ctx.places) is None:
        return
    ranking = permits.ranking(ctx)
    offered = [permits.read_task(ctx, t) for t in proposals.read(ctx.places).open if t and t != "none"]
    for task in sorted(offered, key=lambda t: order.seat_rank(t, ranking)):
        if _free(ctx, permits.needed_role(ctx, task.id)) > 0:
            owner.approve(ctx, task.id, by="YOLO mode")
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
