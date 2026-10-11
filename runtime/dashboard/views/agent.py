"""One agent's panel, what stopping it would stop, and a retired agent's read-only History record."""

from __future__ import annotations

from typing import Any

from runtime.dashboard.views import messaging
from runtime.dashboard.views.agents import history_row, in_tree
from runtime.dashboard.views.kit import PartError, Parts, Query, Snap, epoch, version
from runtime.dashboard.views.shell import factory_state
from runtime.dashboard.views.words import agentwords as words
from runtime.shared.models import DEPRIORITIZED, RETIRED

WANTS = ("settings", "sessions", "queue", "cost", "factory")
_DEPRI = "Deprioritized. It holds no slot, keeps its full context, and comes back first when a slot frees."
_RETIRED = "Retired. It can never be resumed. It moves to History in a few minutes."


def _row(snap: Snap, query: Query) -> dict[str, Any]:
    ref = query.get("id", "")
    row = snap.get("sessions").get(ref)
    if row is None:
        raise PartError(f"There is no agent {ref}.")
    return dict(row)


def agent_version(row: dict[str, Any]) -> str:
    """What a stop carries: the agent as the owner saw it."""
    return version(row["id"], row["state"], row["task"], row["parent"])


def panel(snap: Snap, row: dict[str, Any]) -> dict[str, Any]:
    rows = words.by_id(snap)
    end = epoch(row["ended"]) if row["ended"] else snap.now
    chat_url, chat_note = messaging.chat(row)
    kids = [k for k in words.kids(rows, row) if in_tree(snap, k)]
    life = None
    if row["state"] == DEPRIORITIZED:
        life = {"kind": "depri", "text": _DEPRI}
    elif row["state"] == RETIRED:
        life = {"kind": "retired", "text": _RETIRED}
    actions: list[str] = []
    if row["state"] != RETIRED:
        orchestrator = row["role"] == "orchestrator"
        paused = factory_state(snap) == "paused"
        actions = (["stop-factory"] if paused else ["pause-factory", "stop-factory"]) if orchestrator else ["stop"]
    return {
        "name": words.name(snap, row),
        "family": row["family"],
        "initials": words.initials(row),
        "role": words.role(row),
        "doing": words.doing(snap, row),
        "life_note": life,
        "kv": [
            {"label": "Model", "value": words.model(row)},
            {"label": "Effort", "value": row["effort"]},
            {"label": "Running", "value": f"{max(0, round((end - epoch(row['started'])) / 60))} min"},
            {"label": "Cost", "value": words.cost(snap, row)},
        ],
        "task": row["task"] or None,
        "kids": [
            {
                "id": k["id"],
                "family": k["family"],
                "initials": words.initials(k),
                "name": words.name(snap, k),
                "doing": words.doing(snap, k),
                "cost": words.cost(snap, k),
            }
            for k in kids
        ],
        "thread": messaging.thread(snap, row),
        "message": messaging.box(snap, row),
        "chat_url": chat_url,
        "chat_note": chat_note,
        "actions": actions,
        "version": agent_version(row),
    }


def stopping(snap: Snap, row: dict[str, Any]) -> list[dict[str, Any]]:
    """Everyone a stop retires: the whole factory for the orchestrator, else the agent and all under it."""
    rows = words.by_id(snap)
    if row["role"] == "orchestrator":
        return [r for r in rows.values() if r["state"] != RETIRED]
    out, todo = [], [row]
    while todo:
        current = todo.pop(0)
        if current["state"] != RETIRED:
            out.append(current)
        todo += [r for r in rows.values() if r["parent"] == current["id"]]
    return out


def preview(snap: Snap, row: dict[str, Any]) -> dict[str, Any]:
    rows = stopping(snap, row)
    listed = [
        {
            "family": r["family"],
            "initials": words.initials(r),
            "name": words.name(snap, r),
            "role": words.role(r),
            "model": words.model(r),
            "cost": words.cost(snap, r),
        }
        for r in rows
    ]
    if row["role"] == "orchestrator":
        return {
            "title": "Stop the factory?",
            "text": "Every agent stops now. Their work is kept, and each task goes "
            "back to Proposed next for you to start again.",
            "rows": listed,
            "button": "Stop the factory",
            "coded": True,
        }
    name = words.name(snap, row)
    text = (
        f"These {len(rows)} agents stop now: {name} and everything under it."
        if len(rows) > 1
        else ("This agent stops now." if rows else "This agent has already stopped.")
    )
    if row["task"] and not row["parent"] and row["role"] in ("implementer", "designer") and rows:
        text += f" Its task, {row['task']}, goes back to the top of Proposed next with its work kept."
    return {
        "title": f"Stop {name}?",
        "text": text,
        "rows": listed if len(rows) > 1 else [],
        "button": f"Stop {len(rows)} agents" if len(rows) > 1 else "Stop it",
        "coded": False,
    }


def record(snap: Snap, row: dict[str, Any]) -> dict[str, Any]:
    if row["state"] != RETIRED:
        raise PartError(f"{words.name(snap, row)} is still running; History holds only retired agents.")
    return {**history_row(snap, row), "note": row["note"] or None}


def wants(query: Query) -> dict[str, Any]:
    # The thread is read from the agent's transcript, so only for the panel that is open.
    if query.get("view", "agent") == "agent":
        return {"want": [*WANTS, "thread"], "days": 1, "session": query.get("id", "")}
    return {"want": list(WANTS), "days": 1}


def parts(snap: Snap, query: Query) -> Parts:
    view = query.get("view", "agent")
    build = {"agent": panel, "preview": preview, "history": record}
    if view not in build:
        raise PartError("No such agent view.")
    return {view: lambda: build[view](snap, _row(snap, query))}
