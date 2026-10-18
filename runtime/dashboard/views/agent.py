"""One agent's panel, with what a benched implementer's task waits on; what stopping it would stop; and a retired
agent's read-only History record."""

from __future__ import annotations

from typing import Any

from runtime.dashboard.views import messaging
from runtime.dashboard.views.agents import history_row, in_tree
from runtime.dashboard.views.kit import PartError, Parts, Query, Snap, ago, epoch, plural, version
from runtime.dashboard.views.shell import factory_state
from runtime.dashboard.views.words import agentwords as words
from runtime.dashboard.views.words import taskwords
from runtime.queue.tasks import ON_HOLD
from runtime.queue.turn import MERGED
from runtime.shared.models import DEPRIORITIZED, RETIRED
from runtime.warden.verbs import REGRESSION

WANTS = ("settings", "sessions", "queue", "cost", "factory", "yolo")
_WAITS = ("bundle", "proposals")
_DEPRI = "Deprioritized. It holds no slot and keeps its full context."
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
        life = {"icon": "clock", "text": _DEPRI}
    elif row["state"] == RETIRED:
        life = {"icon": "lock-closed", "text": _RETIRED}
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
        "waits": waits(snap, row),
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


def waits(snap: Snap, row: dict[str, Any]) -> list[dict[str, Any]]:
    """What a live implementer's task waits on, a line each: its merge's check, or each task its hold waits for."""
    found = taskwords.tasks(snap)
    own = found.get(row["task"]) if row["role"] == "implementer" and row["state"] != RETIRED else None
    if own is not None and own["status"] == MERGED:
        return [{"task": None, "title": None, **_check(snap, own)}]
    if own is None or own["status"] != ON_HOLD:
        return []
    lines: list[dict[str, Any]] = []
    for after in own["after"]:
        waited = found.get(after)
        if waited is None:
            lines.append({"task": None, "title": after, "tone": "ring", "text": words.NOT_IN_QUEUE})
            continue
        if waited["status"] == MERGED:
            line = _check(snap, waited)
        else:
            status = taskwords.status(snap, waited)
            line = {"tone": status["tone"], "text": status["word"]}
        lines.append({"task": after, "title": waited["title"], **line})
    return lines


def _check(snap: Snap, task: dict[str, Any]) -> dict[str, str]:
    """The check of the bundle the task's merge is in: running on staging now, else the newest one saved whose walk had
    it in, else where it waits among the merges to check."""
    try:
        bundle = snap.get("bundle")
    # telemetry: surfaced — `_said` puts why in the line the panel draws, and the rest of the panel still draws
    except PartError as exc:
        return _said("unread", why=str(exc))
    place = next((n for n, m in enumerate(bundle["waiting"]) if m["task"] == task["id"]), None)
    if place is None:
        return _said("no-commit")
    merge, since, now = bundle["waiting"][place], _walk_since(snap), snap.now
    if since is not None and merge["on_staging"] is None:
        return _said("unsure")
    if since is not None and merge["on_staging"]:
        return _said("running", sha=bundle["staging"][:7], ago=ago(since, now))
    last = next((c for c in snap.get("queue")["checks"] if c["id"] == merge["check"]), None)
    if last is None:
        return _said("next") if place < bundle["cap"] else _said("later", ahead=plural(place, "merge"))
    failed = [r["scenario"] for r in last["rows"] if r["result"] == "fail"]
    blocked = [r["scenario"] for r in last["rows"] if r["result"] != "pass"]
    kind, names = ("fail", failed) if failed else ("blocked", blocked) if blocked else ("pass", [])
    return _said(kind, sha=last["commit"][:7], ago=ago(epoch(last["at"]), now), scenarios=", ".join(names))


def _said(kind: str, **values: str) -> dict[str, str]:
    tone, text = words.CHECK[kind]
    return {"tone": tone, "text": text.format(**values)}


def _walk_since(snap: Snap) -> float | None:
    """When the regression walk running now began: a tester still walking when a check is saved reports into the
    next one, as the evidence takes its runs. None when no regression tester runs."""
    last = max((epoch(c["at"]) for c in snap.get("queue")["checks"]), default=0.0)
    testers = [s for s in snap.get("sessions").values() if s["role"] == REGRESSION]
    walk = [s for s in testers if s["state"] != RETIRED or (s["ended"] and epoch(s["ended"]) >= last)]
    if not any(s["state"] != RETIRED for s in walk):
        return None
    return min(epoch(s["started"]) for s in walk)


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
            "confirm": True,
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
        "confirm": False,
    }


def record(snap: Snap, row: dict[str, Any]) -> dict[str, Any]:
    if row["state"] != RETIRED:
        raise PartError(f"{words.name(snap, row)} is still running; History holds only retired agents.")
    return {**history_row(snap, row), "note": row["note"] or None}


def wants(query: Query) -> dict[str, Any]:
    # Only for the panel that is open: the thread, from the agent's transcript, and the merges to check, for waits.
    if query.get("view", "agent") == "agent":
        return {"want": [*WANTS, "thread", *_WAITS], "days": 1, "session": query.get("id", "")}
    return {"want": list(WANTS), "days": 1}


def parts(snap: Snap, query: Query) -> Parts:
    view = query.get("view", "agent")
    build = {"agent": panel, "preview": preview, "history": record}
    if view not in build:
        raise PartError("No such agent view.")
    return {view: lambda: build[view](snap, _row(snap, query))}
