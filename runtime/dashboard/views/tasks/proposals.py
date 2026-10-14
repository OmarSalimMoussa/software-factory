"""Home's Proposed next and Also needs you: the orchestrator's picks in fixed places, and every other decision.

A place keeps its size whatever it holds: a card, the moment after the owner acted on one ("settled"), the
orchestrator choosing, or nothing else to propose. A decision card the owner answered says so ("done") for the same
moment, dashboard.settled_seconds, so the owner sees what happened before it moves on.
"""

from __future__ import annotations

import threading
from typing import Any

from runtime.dashboard.views.kit import Snap, brief_url, broken, plural, spelled, version
from runtime.dashboard.views.words import agentwords, taskwords

_settled: dict[int, dict[str, Any]] = {}
_done: dict[str, tuple[float, str]] = {}
_lock = threading.Lock()
SETTLED_WORDS = {
    "approve": ("approved", "done", "check", "Approved. It starts when a slot is free."),
    "later": ("later", "later", "clock", "Back in the queue until something changes."),
}
DONE_WORDS = {
    "go": "Approved. The build starts next.",
    "designer": "Sent to a designer at rung 3.",
    "park": "Parked until the goal changes.",
    "answer": "Your answer is on its way to the session.",
}


def card_version(snap: Snap, task_id: str) -> str:
    """What a proposal's actions carry: the task as proposed and its place."""
    task, places = taskwords.tasks(snap).get(task_id), snap.get("proposals")["open"]
    return version(task, places.index(task_id) if task and task_id in places else None)


def need_version(card: dict[str, Any]) -> str:
    return version(card)


def done_words(snap: Snap, action: str) -> str:
    if action == "extend":
        n = snap.settings.get_int("rounds.extend_by")
        return f"{spelled(n).capitalize()} more {'round' if n == 1 else 'rounds'} granted."
    return DONE_WORDS[action]


def settle(snap: Snap, ref: str, action: str) -> None:
    """Remember, for a moment, what the owner just did to the card ``ref``: a proposal's place, or a decision."""
    until = snap.now + snap.settings.get_int("dashboard.settled_seconds")
    if action not in SETTLED_WORDS:
        with _lock:
            _done[ref] = (until, done_words(snap, action))
        return
    found = snap.get("proposals")["open"]
    task = taskwords.tasks(snap).get(ref)
    task_id = ref
    if task_id not in found or task is None:
        return
    state, look, mark, message = SETTLED_WORDS[action]
    with _lock:
        _settled[found.index(task_id)] = {
            "kind": "settled",
            "task": task_id,
            "line": task["title"],
            "picture": _picture(task),
            "state": state,
            "look": look,
            "icon": mark,
            "message": message,
            "until": until,
        }


def _picture(task: dict[str, Any]) -> str | None:
    return f"/api/picture/task/{task['id']}" if task["picture"] else None


def _card(snap: Snap, task: dict[str, Any], number: int, ids: list[str]) -> dict[str, Any]:
    settings = snap.settings
    n = taskwords.rung(task, settings)
    approved_run = task["run"]
    position = ids.index(task["id"])
    return {
        "kind": "card",
        "task": task["id"],
        "number": number,
        "goal_item": task["goal_item"] or None,
        "severe": task["severity"] == "severe",
        "line": task["title"],
        "why": task["why"],
        "picture": _picture(task),
        "run": {
            "value": str(approved_run if approved_run is not None else n),
            "options": taskwords.run_options(settings),
        },
        "testers": taskwords.testers_word(task, n),
        "estimate": taskwords.estimate(snap, n),
        "run_changed": task["run_changed"],
        "can_up": position > 0,
        "can_down": position < len(ids) - 1,
        "version": card_version(snap, task["id"]),
    }


def slots(snap: Snap) -> dict[str, Any]:
    settings = snap.settings
    limit = settings.get_int("routing.max_proposals")
    found = snap.get("proposals")
    tasks = taskwords.tasks(snap)
    ids = [e for e in found["open"] if e and e != "none" and e in tasks]
    choosing = found["choosing"]  # only an orchestrator at work chooses, as the warden says
    with _lock:
        for index in [i for i, s in _settled.items() if s["until"] <= snap.now]:
            del _settled[index]
        settled = {i: {k: v for k, v in s.items() if k != "until"} for i, s in _settled.items()}
    places: list[dict[str, Any]] = []
    # Every live proposal is drawn: one that came back past a full page, and one whose place a crash between its two
    # writes never recorded, as much as the rest. Only empty places stop at the page's own number.
    orphans = sorted(i for i, task in tasks.items() if task["status"] == "proposed" and i not in found["open"])
    ids += orphans
    for index, entry in enumerate([*found["open"], *orphans]):
        if entry is None or entry == "none":
            if index >= limit:
                continue
            if index in settled:
                places.append(settled[index])
            elif entry is None and choosing:
                places.append({"kind": "choosing"})
            elif entry is None:
                places.append({"kind": "none", "message": ""})
            else:
                places.append({"kind": "none", "message": "Nothing else worth proposing now."})
        elif entry not in tasks or tasks[entry]["status"] != "proposed":
            places.append(broken(entry, f"{entry} holds a place but is not a proposal now."))
        else:
            places.append(_card(snap, tasks[entry], ids.index(entry) + 1, ids))
    while len(places) < limit:
        places.append({"kind": "choosing"} if choosing else {"kind": "none", "message": ""})
    return {"max": limit, "open": len(ids), "approved_waiting": len(found["approved"]), "slots": places}


def needs(snap: Snap) -> dict[str, Any]:
    tasks = taskwords.tasks(snap)
    sessions = snap.get("sessions")
    with _lock:
        done = {ref: words for ref, (until, words) in _done.items() if until > snap.now}
    out = []
    for card in snap.get("cards"):
        if card["answered"] and card["id"] not in done:
            continue
        task = tasks.get(card["task"]) if card["task"] else None
        row: dict[str, Any] = {
            "id": card["id"],
            "kind": card["kind"],
            "task": card["task"] or None,
            "goal_item": (task["goal_item"] or None) if task else None,
            "title": card["title"],
            "sub": card["ask"],
            "chips": [],
            "picture": f"/api/picture/card/{card['id']}" if card["picture"] else None,
            "picture_label": None,
            "brief_url": None,
            "comments": None,
            "actions": [],
            "done": done.get(card["id"]) if card["answered"] else None,
            "version": need_version(card),
        }
        if card["kind"] == "spec":
            row.update(
                chips=[{"text": "Second sign-off", "tone": "warn"}, {"text": "designer's mock-ups", "tone": "violet"}],
                picture_label="See the designer's mock-ups",
                brief_url=brief_url(card["task"]) if card["brief"] else None,
                actions=["brief", "feedback", "go"],
            )
            if card["open_comments"]:
                row["comments"] = f"{plural(card['open_comments'], 'comment')} waiting for the designer"
        elif card["kind"] == "decide":
            row.update(
                title=f"{card['title']} needs your decision",
                picture_label="See how the task moved",
                actions=["extend", "designer", "park"],
            )
        else:
            asker = sessions.get(card["asked_by"])
            if asker is not None:
                row["title"] = f"{agentwords.name(snap, asker)} asks you a question"
            row.update(
                chips=[{"text": "Question", "tone": "info"}], picture_label="See the picture", actions=["answer"]
            )
        row["actions"] = [{"key": key, **taskwords.NEED_ACTIONS[key]} for key in row["actions"]]
        out.append(row)
    return {"count": len(out), "cards": out}
