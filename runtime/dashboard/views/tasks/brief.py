"""A task's brief: its saved versions, the one shown, the owner's comment threads on it, and the sign-off card that
waits on it. The page draws the brief itself in a frame of its own (brief_frame), beside these threads."""

from __future__ import annotations

from typing import Any

from runtime.dashboard.views.kit import PartError, Parts, Query, Snap, brief_url, epoch, plural, version, when
from runtime.queue.formats import TASK_ID

WANTS = ("brief", "cards", "queue")
_STATES = {
    "open": ("waiting for the designer", "warn"),
    "answered": ("answered", "good"),
    "resolved": ("resolved", ""),
}


def _id(query: Query) -> str:
    task_id = query.get("id", "")
    if not TASK_ID.match(task_id):
        raise PartError(f"{task_id!r} is not a task id.")
    return task_id


def wants(query: Query) -> dict[str, Any]:
    return {"want": list(WANTS), "task": _id(query)}


def parts(snap: Snap, query: Query) -> Parts:
    return {"brief": lambda: page(snap, _id(query), query.get("v", ""))}


def _thread(t: dict[str, Any]) -> dict[str, Any]:
    word, tone = _STATES[t["state"]]
    return {
        "id": t["id"],
        "on": f"v{t['version']}",
        "quote": t["quote"],
        "prefix": t["prefix"],
        "suffix": t["suffix"],
        "note": t["note"],
        "when": when(epoch(t["at"])),
        "state": word,
        "tone": tone,
        "replies": [{"who": "Designer", "text": r["text"], "when": when(epoch(r["at"]))} for r in t["replies"]],
        "can_resolve": t["state"] != "resolved",
    }


def page(snap: Snap, task_id: str, asked: str) -> dict[str, Any]:
    brief = snap.get("brief")
    task = next((t for t in snap.get("queue")["tasks"] if t["id"] == task_id), None)
    title = task["title"] if task else task_id
    saved = brief["versions"]
    shown = int(asked) if asked.isdigit() and 1 <= int(asked) <= len(saved) else len(saved)
    card = next((c for c in snap.get("cards") if c["kind"] == "spec" and c["task"] == task_id and not c["answered"]),
                None)  # fmt: skip
    waiting = sum(1 for t in brief["threads"] if t["state"] == "open")
    return {
        "task": task_id,
        "title": title,
        "empty": None if saved else "No brief saved yet. Its designer saves one with factory brief save.",
        "sub": f"{task_id} · {plural(len(saved), 'version')}" if saved else task_id,
        "versions": [
            {
                "label": f"v{v['n']}",
                "when": when(epoch(v["at"])),
                "url": f"{brief_url(task_id)}?v={v['n']}",
                "on": v["n"] == shown,
            }
            for v in saved
        ],  # fmt: skip
        "shown": shown,
        "frame": f"/api/brief-page/{task_id}/{shown}" if saved else None,
        "comment_url": f"/api/brief/{task_id}/comment",
        "resolve_url": f"/api/brief/{task_id}/resolve",
        "threads": [_thread(t) for t in brief["threads"]],
        "card": None
        if card is None
        else {
            "version": version(card),
            "feedback_url": f"/api/needs/{card['id']}/feedback",
            "feedback_label": f"Send feedback · {waiting}" if waiting else "Send feedback",
            "go_url": f"/api/needs/{card['id']}/go",
        },
    }
