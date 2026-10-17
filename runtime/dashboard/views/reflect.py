"""Self-reflect: what agents wrote about the factory itself, newest first, by state and by kind, under how many entries
are open and how many new ones came each day. The owner, or Claude for them, marks an entry fixed or ignored."""

from __future__ import annotations

from collections import Counter
from typing import Any

from runtime.dashboard.views.kit import REFLECT_TONES, PartError, Parts, Query, Snap, day_label, epoch, iso, page, when
from runtime.warden.owner.reflections import FIXED, IGNORED, OPEN

WANTS = ("settings", "reflections")
STATES = {OPEN: "Open", FIXED: "Fixed", IGNORED: "Ignored", "all": "All"}
ACTIONS = {OPEN: [(FIXED, "Fixed"), (IGNORED, "Ignore")], FIXED: [(OPEN, "Reopen")], IGNORED: [(OPEN, "Reopen")]}
MARKED = {FIXED: "Marked fixed.", IGNORED: "Ignored.", OPEN: "Reopened."}


def _days(snap: Snap, count: int, back: int = 0) -> list[str]:
    return [iso(snap.now - n * 86400)[:10] for n in range(back + count - 1, back - 1, -1)]


def _health(snap: Snap, entries: list[dict[str, Any]]) -> dict[str, Any]:
    new = Counter(e["at"][:10] for e in entries)
    days = _days(snap, snap.settings.get_int("reflect.chart_days"))
    top = max(new[d] for d in days)
    week, before = (sum(new[d] for d in _days(snap, 7, back)) for back in (0, 7))
    return {
        "open": sum(1 for e in entries if e["state"] == OPEN),
        "bars": [{"count": new[d], "share": new[d] / top if top else 0.0} for d in days],
        "from": day_label(days[0]),
        "to": "today",
        "caption": f"{week} this week, {before} the week before",
    }


def reflect(snap: Snap, query: Query) -> dict[str, Any]:
    entries = list(reversed(snap.get("reflections")))
    state, kind = query.get("state", OPEN), query.get("kind", "all")
    if state not in STATES:
        raise PartError(f"state must be one of {', '.join(STATES)}.")
    if kind != "all" and kind not in REFLECT_TONES:
        raise PartError(f"kind must be all or one of {', '.join(REFLECT_TONES)}.")
    in_state = [e for e in entries if state in ("all", e["state"])]
    states = [{"key": k, "label": label, "count": sum(1 for e in entries if k in ("all", e["state"]))}
              for k, label in STATES.items()]  # fmt: skip
    kinds = [{"key": k, "count": sum(1 for e in in_state if e["kind"] == k), "tone": tone}
             for k, tone in REFLECT_TONES.items()]  # fmt: skip
    chosen = [e for e in in_state if kind in ("all", e["kind"])]
    chunk, more = page(chosen, query, snap.settings.get_int("dashboard.page_size"))
    rows = [
        {
            "id": e["id"],
            "kind": e["kind"],
            "tone": REFLECT_TONES[e["kind"]],
            "who": e["who"],
            "where": e["where"],
            "when": when(epoch(e["at"])),
            "repeats": e["repeats"],
            "text": e["text"],
            "state": None if e["state"] == OPEN else STATES[e["state"]],
            "note": e["note"],
            "came_back": e["came_back"],
            "actions": [{"state": s, "label": label} for s, label in ACTIONS[e["state"]]],
        }
        for e in chunk
    ]
    return {"health": _health(snap, entries), "states": states, "kinds": kinds, "entries": rows, "more": more}


def wants(query: Query) -> dict[str, Any]:
    return {"want": list(WANTS)}


def parts(snap: Snap, query: Query) -> Parts:
    return {"reflect": lambda: reflect(snap, query)}
