"""The words for an agent wherever it shows: its name, its role, what it is doing, its state and its cost."""

from __future__ import annotations

import re
from typing import Any

from runtime.dashboard.views.kit import INITIALS, Snap, cost_of, model_label
from runtime.shared.models import BENCH_PENDING, DEPRIORITIZED, RETIRED, REVIVING, WAITING, WORKING

_TASK = re.compile(r"\bt-\d{4,}\b")
_ROLE_WORDS = {"orchestrator": "orchestrator", "designer": "designer", "intake": "intake", "triage": "triage"}
# Marker and state word for each live state; a retired agent shows how it ended.
_LIVE = {
    WORKING: ("working", "working"),
    WAITING: ("waiting", "waiting for you"),
    BENCH_PENDING: ("depri", "moving to the bench"),
    DEPRIORITIZED: ("depri", "deprioritized"),
    REVIVING: ("working", "coming back"),
}
_ENDED = {
    "finished": ("done", "done"),
    "stopped": ("stop", "stopped"),
    "with-parent": ("stop", "stopped"),
    "crashed": ("stop", "crashed"),
}
_KID_ORDER = ("intake", "impl", "design", "qa", "orch")
WHY = {
    "finished": ("finished", "good"),
    "with-parent": ("stopped with its parent", "bad"),
    "crashed": ("crashed", "bad"),
}


def name(snap: Snap, row: dict[str, Any]) -> str:
    """The orchestrator, a task's title for its implementer or designer, a subagent's kind."""
    if row["role"] == "orchestrator":
        return "Orchestrator"
    if row["role"] in ("implementer", "designer") and row["task"]:
        task = next((t for t in snap.get("queue")["tasks"] if t["id"] == row["task"]), None)
        if task is not None:
            return str(task["title"])
    if row["role"] in ("intake", "triage"):
        return str(row["role"]).title()
    return str(row["role"])


def role(row: dict[str, Any]) -> str:
    kind = str(row["role"])
    if kind == "implementer":
        return {1: "implementer, small", 2: "implementer, large"}.get(row["rung"], "implementer")
    if kind.startswith("qa-"):
        return "tester"
    if kind.startswith("critic-"):
        return "critic"
    return _ROLE_WORDS.get(kind, kind)


def initials(row: dict[str, Any]) -> str:
    return INITIALS[row["family"]]


def by_id(snap: Snap) -> dict[str, dict[str, Any]]:
    return dict(snap.get("sessions"))


def doing(snap: Snap, row: dict[str, Any]) -> str:
    state = row["state"]
    if state == DEPRIORITIZED:
        if row["note"] == "paused with the factory":
            return "Paused with the factory · comes back when you resume"
        found = _TASK.search(row["note"])
        task = next((t for t in snap.get("queue")["tasks"] if found and t["id"] == found.group(0)), None)
        return f"Pushed out for “{task['title']}” · comes back first" if task else "Pushed out · comes back first"
    if state == BENCH_PENDING:
        return "Moving to the bench, committing its work"
    if state == REVIVING:
        return "Coming back, rebuilding its slot"
    if row["sentence"] is None:
        return "What it is doing could not be read"
    if row["sentence"]:
        return str(row["sentence"])
    return "Waiting for you" if state == WAITING else "Working"


def marker(row: dict[str, Any]) -> tuple[str, str]:
    """The tree's marker and the state word."""
    if row["state"] == RETIRED:
        return _ENDED[row["retired"]]
    return _LIVE[row["state"]]


def why(row: dict[str, Any]) -> dict[str, str]:
    """How a retired agent ended, as a toned pill."""
    if row["retired"] == "stopped":
        return {"text": "stopped by you" if row["retired_by"] == "owner" else "stopped", "tone": "bad"}
    text, tone = WHY[row["retired"]]
    return {"text": text, "tone": tone}


def cost(snap: Snap, row: dict[str, Any]) -> str:
    totals = snap.get("cost")["totals"]["agent"]
    return cost_of(totals.get(row["agent_id"] or row["id"]))


def model(row: dict[str, Any]) -> str:
    return model_label(str(row["model"]))


def started_by(snap: Snap, row: dict[str, Any]) -> str:
    parent = snap.get("sessions").get(row["parent"]) if row["parent"] else None
    if parent is not None:
        return f"started by {name(snap, parent)}"
    if row["started_by"] == "owner":
        return "started by you"
    return f"started by {row['started_by'].title()}" if row["started_by"] else "started by the factory"


def kids(rows: dict[str, dict[str, Any]], parent: dict[str, Any]) -> list[dict[str, Any]]:
    """Who works under an agent: its subagents; under the orchestrator, every implementer and designer too."""
    under = [r for r in rows.values() if r["parent"] == parent["id"]]
    if parent["role"] == "orchestrator":
        under += [r for r in rows.values() if not r["parent"] and r["role"] in ("implementer", "designer")]
    return sorted(under, key=lambda r: (_KID_ORDER.index(r["family"]), r["started"]))
