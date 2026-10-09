"""The proposal places and the owner's approved order, kept in the warden's proposals.json (plan 4.1).

``open`` holds one entry per place on Home: a task id, None while the orchestrator is choosing, or "none" when it
found nothing else worth proposing. A card that leaves turns its place into None, so nothing below it moves.
``approved`` is the owner's order of approved runs, which start from the top.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypeVar

from runtime.shared import store
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused

NONE = "none"
T = TypeVar("T")


@dataclass(slots=True)
class Proposals:
    open: list[str | None] = field(default_factory=list)
    approved: list[str] = field(default_factory=list)


def read(places: Places) -> Proposals:
    raw = store.read_json_or(places.proposals, {"open": [], "approved": []})
    if (
        not isinstance(raw, dict)
        or set(raw) != {"open", "approved"}
        or not all(e is None or isinstance(e, str) for e in raw["open"])
        or not all(isinstance(e, str) for e in raw["approved"])
    ):
        raise store.StoreError(f"{places.proposals}: not open and approved lists of task ids")
    return Proposals(list(raw["open"]), list(raw["approved"]))


def write(places: Places, found: Proposals) -> None:
    store.write_json(places.proposals, {"open": found.open, "approved": found.approved}, mode=0o600)


def place(found: Proposals, entry: str, limit: int) -> int:
    """Put a task id or NONE in the first free place; refused when ``limit`` places hold proposals."""
    for i, held in enumerate(found.open):
        if held is None or held == NONE:
            found.open[i] = entry
            return i
    if len(found.open) >= limit:
        raise Refused("max-proposals", f"routing.max_proposals ({limit}) proposals are open; wait for the owner")
    found.open.append(entry)
    return len(found.open) - 1


def place_top(found: Proposals, task_id: str) -> None:
    """A run that comes back (stopped, voided, a rung up) goes first, taking a free place if there is one; it is
    never kept off the page for want of a place."""
    free = next((i for i, held in enumerate(found.open) if held is None or held == NONE), None)
    if free is not None:
        del found.open[free]
    found.open.insert(0, task_id)


def remove(found: Proposals, task_id: str) -> bool:
    """Take a task out of both lists; whether it held a proposal place, which is now free."""
    held = task_id in found.open
    found.open = [None if e == task_id else e for e in found.open]
    found.approved = [e for e in found.approved if e != task_id]
    return held


def reorder(order: list[T], task_id: str, direction: str, seen: object) -> None:
    """Move one entry up or down, only when the device saw the order as it is now (R29)."""
    if seen != list(order):
        raise Refused("stale", f"This changed; here is what it is now: {list(order)}")
    if task_id not in order or direction not in ("up", "down"):
        raise Refused("move", f"{task_id} cannot move {direction}")
    i = next(n for n, entry in enumerate(order) if entry == task_id)
    j = i - 1 if direction == "up" else i + 1
    if not 0 <= j < len(order):
        raise Refused("move", f"{task_id} is already at the {'top' if direction == 'up' else 'bottom'}")
    order[i], order[j] = order[j], order[i]
