"""The words for a task wherever it shows: its status as a dot and a word, its rung, its run, its testers, its cost
and its estimate, and how the checks that cleared it went, all from the snapshot and the settings in effect.
"""

from __future__ import annotations

import dataclasses
import statistics
from typing import Any

from runtime.dashboard.views.kit import SURFACES, Snap, cost_of, model_word, ordinal
from runtime.qa import plan as qa_plan
from runtime.queue import route
from runtime.shared import models
from runtime.shared.models import ON_STAGING, QA_CLEAN, RETIRED, Task
from runtime.shared.protocol import Refused
from runtime.shared.settings import Settings

STAGES = ["Request", "Triage", "Proposed", "Approved", "Design", "Build", "QA", "Merge", "Check", "Staging", "Prod"]
_AT = {
    "queued": 1,
    "parked": 1,
    "proposed": 2,
    "approved": 3,
    "designing": 4,
    "review": 4,
    "building": 5,
    "deprioritized": 5,
    "decide": 5,
    "blocked": 5,
    "on-hold": 5,
    "qa": 6,
    QA_CLEAN: 6,
    "turn": 7,
    "merged": 7,
    "checked": 8,
    ON_STAGING: 9,
    "promoted": 10,
}
# Each task status's tone, the one table every page draws a status by.
TONES = {
    "queued": "ring", "parked": "ring", "proposed": "warn", "approved": "good", "designing": "accent",
    "review": "warn", "building": "accent", "deprioritized": "warn", "decide": "warn", "qa": "accent",
    QA_CLEAN: "accent", "turn": "accent", "merged": "accent", "checked": "good", ON_STAGING: "good",
    "promoted": "good", "blocked": "bad", "on-hold": "ring", "closed": "ring",
}  # fmt: skip
_WORDS = {
    "queued": "Queued",
    "parked": "Parked",
    "proposed": "Waiting for you",
    "designing": "Designing",
    "review": "Needs your Go",
    "deprioritized": "Deprioritized, back first",
    QA_CLEAN: "QA passed, waiting to merge",
    "turn": "Merging",
    "merged": "Merged, being checked",
    "checked": "Checked, on its way to staging",
    ON_STAGING: "On staging",
    "promoted": "On prod",
    "decide": "Needs your decision",
    "blocked": "Blocked until you fix it",
    "on-hold": "Waiting for another task's check to pass",
    "closed": "Closed",
}
NEED_ACTIONS: dict[str, dict[str, Any]] = {
    "brief": {"label": "Open brief", "icon": "document-text", "primary": False},
    "feedback": {"label": "Send feedback", "icon": "chat-bubble-left-right", "primary": False},
    "go": {"label": "Go", "icon": "check", "primary": True},
    "extend": {"label": "Extend", "icon": None, "primary": False},
    "designer": {"label": "To a designer", "icon": None, "primary": False},
    "park": {"label": "Park", "icon": None, "primary": False},
    "answer": {"label": "Send answer", "icon": "check", "primary": True},
    "fixed": {"label": "Fixed, carry on", "icon": "check", "primary": True},
}
_TASK_FIELDS = frozenset(f.name for f in dataclasses.fields(Task))
LANDED = ("merged", "checked", ON_STAGING, "promoted")
IN_PROGRESS = (
    "designing",
    "review",
    "building",
    "deprioritized",
    "qa",
    QA_CLEAN,
    "turn",
    "merged",
    "checked",
    "decide",
    "blocked",
    "on-hold",
)
_TESTER_WORDS = (
    ("UI", ("qa-ui-function", "qa-ux", "qa-visual")),
    ("backend", ("qa-backend",)),
    ("security", ("qa-security",)),
)


def tasks(snap: Snap) -> dict[str, dict[str, Any]]:
    return {t["id"]: t for t in snap.get("queue")["tasks"]}


def as_task(raw: dict[str, Any]) -> Task:
    """The task record itself, without the facts the snapshot adds from its log."""
    return models.load(Task, {k: v for k, v in raw.items() if k in _TASK_FIELDS})


def worker(snap: Snap, task_id: str) -> dict[str, Any] | None:
    """The implementer or designer working on the task now, or the last one that did."""
    rows: list[dict[str, Any]] = [s for s in snap.get("sessions").values() if s["task"] == task_id and not s["parent"]]
    live = [s for s in rows if s["state"] != RETIRED]
    pool = live or rows
    return max(pool, key=lambda s: str(s["started"])) if pool else None


def status(snap: Snap, task: dict[str, Any]) -> dict[str, str]:
    """The task's status as a dot and a word."""
    state = task["status"]
    if state == "approved":
        order = snap.get("proposals")["approved"]
        place = order.index(task["id"]) + 1 if task["id"] in order else None
        return {"tone": TONES[state], "word": f"Approved, {ordinal(place)} in line" if place else "Approved"}
    if state == "building":
        fixing = (snap.state.get("outage") or {}).get("task") == task["id"]
        return {"tone": TONES[state], "word": "Fixing prod" if fixing else "Building"}
    if state == "qa":
        session = worker(snap, task["id"])
        if session is None or not session["facts"].get("qa_round"):
            return {"tone": TONES[state], "word": "In QA"}
        total = snap.settings.get_int("rounds.qa") + session["extensions"].get("qa", 0)
        return {"tone": TONES[state], "word": f"QA, round {session['facts']['qa_round']} of {total}"}
    return {"tone": TONES[state], "word": _WORDS[state]}


def progress_at(task: dict[str, Any], log: list[dict[str, Any]]) -> int:
    if task["status"] != "closed":
        return _AT[task["status"]]
    before = next((e.get("from", "") for e in reversed(log) if e.get("to") == "closed"), "")
    return _AT.get(before, 1)


def rung(task: dict[str, Any], settings: Settings) -> int | None:
    """The planned run, else the lowest rung the ladder allows; None while the task lacks the facts to route."""
    if task["run"] is not None:
        return int(task["run"])
    try:
        return route.route(as_task(task), settings)
    # telemetry: expected — a task without its facts has no rung yet; the orchestrator adds them
    except Refused:
        return None


def rung_word(n: int | None, settings: Settings) -> str:
    if n is None:
        return "not routed"
    if n == 0:
        return "no build"
    return model_word(route.model_for(settings, n)) if n in (1, 2) else "spec"


def route_steps(settings: Settings) -> list[str]:
    return ["0 · no build", f"1 · {rung_word(1, settings)}", f"2 · {rung_word(2, settings)}", "3 · spec", "4 · your Go"]


def run_options(settings: Settings) -> list[list[str]]:
    return [
        ["1", f"Rung 1 · {rung_word(1, settings)}"],
        ["2", f"Rung 2 · {rung_word(2, settings)}"],
        ["3", "Rung 3 · design, then build"],
        ["4", "Rung 4 · design, your Go"],
    ]


def surfaces(task: dict[str, Any]) -> list[str]:
    return [SURFACES[s] for s in task["surfaces"]]


def required_testers(task: dict[str, Any], n: int | None) -> list[str]:
    return list(qa_plan.required(task["surfaces"], n if n is not None else 1, {}))


def testers_word(task: dict[str, Any], n: int | None) -> str:
    needed = set(required_testers(task, n))
    kinds = [word for word, names in _TESTER_WORDS if needed & set(names)]
    if not kinds:
        return "No testers"
    return (" and ".join([", ".join(kinds[:-1]), kinds[-1]]) if len(kinds) > 1 else kinds[0]) + " testers"


def cost_cell(snap: Snap, task: dict[str, Any]) -> list[float] | None:
    """What the task cost so far, with the intake runs of its requests rolled in."""
    totals = snap.get("cost")["totals"]["task"]
    cells = [totals[k] for k in (task["id"], *task["requests"]) if k in totals]
    if not cells:
        return None
    return [sum(c[0] for c in cells), sum(c[1] for c in cells)]


def cost(snap: Snap, task: dict[str, Any]) -> str:
    return cost_of(cost_cell(snap, task))


def estimate(snap: Snap, n: int | None) -> str:
    """The median cost of the last runs at the rung, once enough of them are known."""
    settings = snap.settings
    if n is None:
        return "estimate unknown"
    runs = sorted(
        (t for t in tasks(snap).values() if t["run"] == n and t["status"] in LANDED), key=lambda t: t["created"]
    )
    known = [c[0] for t in runs if (c := cost_cell(snap, t)) is not None and not c[1]]
    known = known[-settings.get_int("routing.estimate_window") :]
    if len(known) < settings.get_int("routing.estimate_min_runs"):
        return "estimate unknown"
    return f"≈ ${statistics.median(known):,.0f}"


def severity_chip(task: dict[str, Any]) -> dict[str, str]:
    return {"text": task["severity"], "tone": {"severe": "bad", "major": "warn"}.get(task["severity"], "")}


def requests_of(snap: Snap, task: dict[str, Any]) -> list[dict[str, Any]]:
    by_id = {r["id"]: r for r in snap.get("queue")["requests"]}
    return [by_id[r] for r in task["requests"] if r in by_id]


def money_or_estimate(snap: Snap, task: dict[str, Any]) -> tuple[str, str]:
    if task["status"] == "proposed":
        return "Estimate", estimate(snap, rung(task, snap.settings))
    return "Cost so far", cost(snap, task)


def carried(row: dict[str, Any]) -> str:
    """Why a check did not walk a scenario again: where the pass it carries came from."""
    return f"it passed at {row['commit'][:7]}, and nothing it depends on has changed since"


def check_summary(passed: int, walked: int, carried: int) -> str:
    if not walked:
        return f"{carried} carried, none walked"
    return f"{passed} of {walked} passed" + (f", {carried} carried" if carried else "")
