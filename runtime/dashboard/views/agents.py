"""Agents: who is doing what, as a tree under the orchestrator, with History of every retired agent below it."""

from __future__ import annotations

from typing import Any

from runtime.dashboard.views.kit import (
    FAMILIES,
    Parts,
    Query,
    Snap,
    broken,
    epoch,
    iso,
    money,
    page,
    plural,
    span,
    when,
)
from runtime.dashboard.views.shell import factory_state
from runtime.dashboard.views.words import agentwords as words
from runtime.shared.models import DEPRIORITIZED, HOLDS_SLOT, RETIRED

WANTS = ("settings", "sessions", "queue", "cost", "factory", "slots", "history")


def _linger(snap: Snap) -> float:
    return snap.settings.get_int("dashboard.retired_linger_minutes") * 60.0


def in_tree(snap: Snap, row: dict[str, Any]) -> bool:
    """Live, or retired so recently it still shows in the tree."""
    return row["state"] != RETIRED or bool(row["ended"]) and snap.now - epoch(row["ended"]) < _linger(snap)


def node(snap: Snap, rows: dict[str, dict[str, Any]], row: dict[str, Any]) -> dict[str, Any]:
    kids = [k for k in words.kids(rows, row) if in_tree(snap, k)]
    marker, state_word = words.marker(row)
    return {
        "id": row["id"],
        "name": words.name(snap, row),
        "role": words.role(row),
        "family": row["family"],
        "initials": words.initials(row),
        "kids_label": f" · {len(kids)}" if kids else "",
        "doing": words.doing(snap, row),
        "model": words.model(row),
        "marker": marker,
        "state_word": state_word,
        "cost": words.cost(snap, row),
        "can_stop": row["state"] != RETIRED,
        "retired": row["state"] == RETIRED,
        "kids": [node(snap, rows, k) for k in kids],
    }


def tree(snap: Snap) -> dict[str, Any] | None:
    rows = words.by_id(snap)
    roots = [r for r in rows.values() if r["role"] == "orchestrator" and in_tree(snap, r)]
    root = max(roots, key=lambda r: (r["state"] != RETIRED, r["started"]), default=None)
    return None if root is None else node(snap, rows, root)


def history_row(snap: Snap, row: dict[str, Any]) -> dict[str, Any]:
    started, ended = epoch(row["started"]), epoch(row["ended"])
    return {
        "id": row["id"],
        "family": row["family"],
        "initials": words.initials(row),
        "name": words.name(snap, row),
        "role": words.role(row),
        "task": row["task"] or None,
        "model": words.model(row),
        "started": when(started),
        "started_by": words.started_by(snap, row),
        "ended": when(ended),
        "lasted": f"after {span(ended - started)}",
        "why": words.why(row),
        "cost": words.cost(snap, row),
    }


def history(snap: Snap, query: Query) -> dict[str, Any]:
    """Every retirement history.jsonl records, newest first, past the tree's linger; a line that cannot be read is
    one failed row, first, and an agent the registry no longer holds says so in its own row."""
    sessions = snap.get("sessions")
    failed: list[dict[str, Any]] = []
    done: list[dict[str, Any]] = []
    for line in reversed(snap.get("history")):
        if "error" in line:
            failed.append(broken(f"history line {line['line']}", line["error"]))
        elif line["session"] not in sessions:
            done.append(broken(line["name"], f"{line['name']} retired, but the registry has no record of it"))
        elif not in_tree(snap, sessions[line["session"]]):
            done.append(sessions[line["session"]])
    chunk, more = page([*failed, *done], query, snap.settings.get_int("dashboard.page_size"), "history_page")
    return {"rows": [r if r.get("broken") else history_row(snap, r) for r in chunk], "more": more}


def capacity(snap: Snap) -> list[dict[str, Any]]:
    settings = snap.settings
    rows = snap.get("sessions").values()
    out = []
    for label, role, cap in (
        ("Implementers", "implementer", "caps.implementers"),
        ("Designers", "designer", "caps.designers"),
    ):
        mine = [r for r in rows if r["role"] == role and not r["parent"]]
        benched = sum(1 for r in mine if r["state"] == DEPRIORITIZED)
        out.append(
            {
                "label": label,
                "value": sum(1 for r in mine if r["state"] in HOLDS_SLOT),
                "max": settings.get_int(cap),
                "note": f"{benched} deprioritized, back first when a slot frees" if benched else None,
            }
        )
    slots = snap.get("slots")["rows"]
    count = settings.get_int("slots.count")
    held = [s["n"] for s in slots if s["lease"] is not None]
    over = [n for n in held if n > count]
    out.append(
        {
            "label": "Slots in use",
            "value": len(held),
            "max": count,
            "note": f"{count} once slot {max(over)} frees" if over else None,
        }
    )
    return out


def legend(snap: Snap) -> list[dict[str, Any]]:
    live = [r for r in snap.get("sessions").values() if r["state"] != RETIRED]
    return [
        {"family": key, "label": label, "count": sum(1 for r in live if r["family"] == key)}
        for key, (label, _) in FAMILIES.items()
    ]


def banner(snap: Snap) -> dict[str, Any] | None:
    state = factory_state(snap)
    if state == "paused":
        return {
            "kind": "paused",
            "text": "The factory is paused.",
            "sub": "Every agent is benched with its work kept. Resume brings them back in order.",
            "action": "resume",
        }
    if state == "stopped":
        return {
            "kind": "stopped",
            "text": "The factory is stopped.",
            "sub": "Nothing new starts until you start the orchestrator.",
            "action": "start",
        }
    return None


def header(snap: Snap) -> dict[str, Any]:
    live = sum(1 for r in snap.get("sessions").values() if r["state"] != RETIRED)
    today = snap.get("cost")["days"].get(iso(snap.now)[:10], {}).get("family", {})
    usd, unknown = sum(c[0] for c in today.values()), int(sum(c[1] for c in today.values()))
    priced = f" · {plural(unknown, 'call')} with unknown price" if unknown else ""
    return {"sub": f"{plural(live, 'agent')} live · {money(usd)} so far today{priced}"}


def wants(query: Query) -> dict[str, Any]:
    return {"want": list(WANTS), "days": 1}


def parts(snap: Snap, query: Query) -> Parts:
    return {
        "header": lambda: header(snap),
        "capacity": lambda: capacity(snap),
        "legend": lambda: legend(snap),
        "banner": lambda: banner(snap),
        "tree": lambda: tree(snap),
        "history": lambda: history(snap, query),
    }
