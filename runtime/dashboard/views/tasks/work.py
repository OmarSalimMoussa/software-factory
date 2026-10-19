"""Work: the tasks in their one order, the requests by inbox, parked work, what is done and the checks of the default
branch."""

from __future__ import annotations

from typing import Any

from runtime.dashboard.views.kit import (
    PartError,
    Parts,
    Query,
    Snap,
    ago,
    broken,
    epoch,
    first_line,
    one_of,
    page,
    plural,
    when,
)
from runtime.dashboard.views.tasks import done as done_tab
from runtime.dashboard.views.words import taskwords
from runtime.queue.formats import outcome
from runtime.shared import order
from runtime.shared.models import ON_STAGING

# A row keeps its key as it moves between these groups and Home's proposals, so it slides instead of jumping.
_FLIP = {"approved": "appr", "proposed": "prop"}

WANTS = (
    "settings",
    "queue",
    "proposals",
    "sessions",
    "cost",
    "outage",
    "factory",
    "probes",
    "goal",
    "done",
    "goal_change",
)
TABS = (("tasks", "Tasks"), ("requests", "Requests"), ("parked", "Parked"), ("done", "Done"), ("checks", "Checks"))
INBOXES = (("all", "All inboxes"), ("agents", "Agents"), ("owner", "You"), ("users", "Users"), ("chat", "Chat"))
OUTCOMES = [["all", "Any outcome"], ["waiting", "Waiting"], ["task", "Task"], ["noise", "Noise"], ["parked", "Parked"]]
_FROM = {
    "agents": ("A", "impl", "from agents"),
    "owner": ("Y", "orch", "from you"),
    "users": ("U", "qa", "from users"),
    "chat": ("C", "intake", "from the chat"),
}
_GROUPS = {
    "progress": ("In progress", None, "Nothing is being built."),
    "approved": (
        "Approved, starting in this order",
        "Each starts when a slot is free. Move one up to start it sooner.",
        "Nothing approved is waiting.",
    ),
    "proposed": (
        "Proposed to you",
        "Nothing starts until you approve it. The full cards, with their sketches, are on Home.",
        "No proposals waiting.",
    ),
    "queue": (
        "Queue, in computed order",
        "The orchestrator proposes from the top of this order. Boost makes a task "
        "the next proposal, ahead of its own picks.",
        "The queue is empty.",
    ),
}


def _row(snap: Snap, task: dict[str, Any], rank: int | None, action: str | None, ids: list[str]) -> dict[str, Any]:
    settings = snap.settings
    n = taskwords.rung(task, settings)
    position = ids.index(task["id"]) if task["id"] in ids else -1
    return {
        "id": task["id"],
        "rank": rank,
        "rank_mark": None if rank is not None else ("!" if task["severity"] == "severe" else "·"),
        "title": task["title"],
        "severity": taskwords.severity_chip(task),
        "surfaces": taskwords.surfaces(task),
        "requests": plural(len(task["requests"]), "request"),
        "goal": task["goal_item"] or None,
        "rung": n,
        "rung_word": taskwords.rung_word(n, settings),
        "status": taskwords.status(snap, task),
        "cost": taskwords.cost(snap, task),
        "action": action,
        "boosted": bool(task["boosted_at"]),
        "can_up": action == "order" and position > 0,
        "can_down": action == "order" and 0 <= position < len(ids) - 1,
    }


def groups(snap: Snap) -> list[dict[str, Any]]:
    queue = snap.get("queue")
    found = snap.get("proposals")
    finished = done_tab.ids(snap)
    tasks = {i: t for i, t in taskwords.tasks(snap).items() if i not in finished}
    approved = [t for t in found["approved"] if t in tasks and tasks[t]["status"] == "approved"]
    proposed = [t for t in found["open"] if t and t in tasks and tasks[t]["status"] == "proposed"]
    model_tasks = {t.id: t for t in (taskwords.as_task(raw) for raw in tasks.values())}
    goals = {i["id"]: i["rank"] for i in snap.get("goal")["items"] if i["rank"] is not None}
    ranking = order.Ranking(found["approved"], goals)

    def ranked(states: tuple[str, ...]) -> list[dict[str, Any]]:
        chosen = [model_tasks[i] for i, t in tasks.items() if t["status"] in states]
        return [tasks[t.id] for t in order.ordered(chosen, ranking)]

    rows: dict[str, list[dict[str, Any]]] = {
        # A task on staging that Done does not list yet is still in progress.
        "progress": [_row(snap, t, None, None, []) for t in ranked((*taskwords.IN_PROGRESS, ON_STAGING))],
        "approved": [_row(snap, tasks[i], n, "order", approved) for n, i in enumerate(approved, 1)],
        "proposed": [_row(snap, tasks[i], n, "approve", []) for n, i in enumerate(proposed, 1)],
        "queue": [broken(b["file"], b["error"]) for b in queue["broken"] if b["file"].startswith("tasks/")]
        + [_row(snap, t, n, _boost(t, snap), []) for n, t in enumerate(ranked(("queued",)), 1)],
    }
    # The approved group shows only while something approved waits, as the prototype draws it.
    return [
        {
            "key": key,
            "title": title,
            "hint": hint,
            "empty": empty,
            "count": len(rows[key]),
            "flip": _FLIP.get(key, "row"),
            "rows": rows[key],
        }
        for key, (title, hint, empty) in _GROUPS.items()
        if key != "approved" or rows[key]
    ]


def _boost(task: dict[str, Any], snap: Snap) -> str | None:
    if taskwords.rung(task, snap.settings) == 0:
        return None
    return "unboost" if task["boosted_at"] else "boost"


def _outcome(request: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The outcome filter's key, and the pill the row shows."""
    found = outcome(request["outcome"])
    if found.task:
        return "task", {"text": found.task, "tone": "accent", "mono": True}
    return found.kind, {"text": found.kind, "tone": "", "mono": False}


def requests(snap: Snap, query: Query) -> dict[str, Any]:
    queue = snap.get("queue")
    inbox = one_of(query.get("inbox", "all"), [k for k, _ in INBOXES], "inbox")
    wanted = one_of(query.get("outcome", "all"), [k for k, _ in OUTCOMES], "outcome")
    boosted = queue["boosts"]
    every = sorted(queue["requests"], key=lambda r: r["at"], reverse=True)
    shown = [r for r in every if inbox in ("all", r["inbox"]) and wanted in ("all", _outcome(r)[0])]
    # A request that cannot be read is a row that says so, first, and counts toward its page like any other.
    failed = [broken(b["file"], b["error"]) for b in queue["broken"] if b["file"].startswith("requests/")]
    chunk, more = page([*failed, *shown], query, snap.settings.get_int("dashboard.page_size"))
    rows = [r if "broken" in r else _request_row(snap, r, boosted) for r in chunk]
    counts = {key: sum(1 for r in every if key in ("all", r["inbox"])) for key, _ in INBOXES}
    return {
        "inboxes": [{"key": k, "label": label, "count": counts[k]} for k, label in INBOXES],
        "outcomes": OUTCOMES,
        "rows": rows,
        "more": more,
    }


def _request_row(snap: Snap, r: dict[str, Any], boosted: dict[str, str]) -> dict[str, Any]:
    initial, family, source = _FROM[r["inbox"]]
    return {
        "id": r["id"],
        "inbox": r["inbox"],
        "initial": initial,
        "family": family,
        "text": first_line(r["body"]),
        "severe": r["severity"] == "severe",
        "age": ago(epoch(r["at"]), snap.now),
        "outcome": _outcome(r)[1],
        "from": source,
        "can_boost": not r["outcome"] and r["id"] not in boosted,
    }


def parked(snap: Snap) -> dict[str, Any]:
    queue = snap.get("queue")
    rows = []
    for t in taskwords.tasks(snap).values():
        if t["status"] == "parked":
            move = t["last_move"]
            rows.append(
                (
                    move["at"] or t["created"],
                    {
                        "id": t["id"],
                        "title": t["title"],
                        "why": move["text"] or "Parked",
                        "requests": plural(len(t["requests"]), "request"),
                        "date": when(epoch(move["at"] or t["created"]))[:-7],
                    },
                )
            )
    for r in queue["requests"]:
        if outcome(r["outcome"]).kind == "parked":
            rows.append(
                (
                    r["at"],
                    {
                        "id": r["id"],
                        "title": first_line(r["body"]),
                        "why": outcome(r["outcome"]).reason,
                        "requests": plural(1, "request"),
                        "date": when(epoch(r["at"]))[:-7],
                    },
                )
            )
    return {"rows": [row for _, row in sorted(rows, key=lambda x: x[0], reverse=True)]}


# Each QA result's dot class on the checks tab: app.css colours no class as a pass, f as a failure, b as blocked.
_RESULT_DOT = {"pass": "", "fail": "f", "blocked": "b"}


def checks(snap: Snap) -> dict[str, Any]:
    queue = snap.get("queue")
    rows: list[dict[str, Any]] = [
        broken(b["file"], b["error"]) for b in queue["broken"] if b["file"].startswith("checks/")
    ]
    for c in sorted(queue["checks"], key=lambda c: c["at"], reverse=True):
        passed = sum(1 for r in c["rows"] if r["result"] == "pass")
        failed = any(r["result"] == "fail" for r in c["rows"])
        rows.append(
            {
                "id": c["id"],
                "merge": c["title"],
                "sha": c["commit"][:7],
                "age": ago(epoch(c["at"]), snap.now),
                "results": [
                    {"scenario": r["scenario"], "state": r["result"], "cls": _RESULT_DOT[r["result"]]}
                    for r in c["rows"]
                ],
                "summary": f"{passed} of {len(c['rows'])} passed",
                "tone": "bad" if failed else "good",
            }
        )
    legend = [{"label": "pass", "tone": "good"}, {"label": "fail", "tone": "bad"}, {"label": "blocked", "tone": ""}]
    return {"legend": legend, "rows": rows}


def header(snap: Snap) -> dict[str, Any]:
    queue = snap.get("queue")
    waiting = sum(1 for r in queue["requests"] if not r["outcome"] and r["severity"] != "severe")
    queued = sum(1 for t in queue["tasks"] if t["status"] == "queued")
    last = snap.get("factory")["triage"]
    line = None
    if last is not None:
        line = (
            f"Last triage, {ago(epoch(last['at']), snap.now)}: {plural(last['batch'], 'request')}, "
            f"{plural(len(last['new']), 'new task')}, {last['joined']} joined, {last['noise']} noise, "
            f"{last['parked']} parked"
        )
    change = snap.get("goal_change")
    return {
        "sub": f"{plural(waiting, 'request')} wait for triage · {plural(queued, 'task')} queued",
        "triage_line": line,
        "goals_line": None if change is None else _goals_line(snap, change),
        "goals_why": None if change is None else ", ".join(change["changed"]),
    }


def _goals_line(snap: Snap, change: dict[str, Any]) -> str:
    back = len(change["requests"]) + len(change["tasks"])
    came = f"{plural(back, 'parked item')} came back for triage" if back else "nothing was parked"
    return f"Goals changed, {ago(epoch(change['at']), snap.now)}: {came}"


def tabs(snap: Snap) -> list[list[Any]]:
    queue = snap.get("queue")
    built = groups(snap)
    counts = {
        "tasks": sum(len(g["rows"]) for g in built),
        "requests": len(queue["requests"]),
        "parked": len(parked(snap)["rows"]),
        "done": len(done_tab.ids(snap)),
        "checks": len(queue["checks"]),
    }
    return [[key, label, counts[key]] for key, label in TABS]


def wants(query: Query) -> dict[str, Any]:
    return {"want": list(WANTS)}


def parts(snap: Snap, query: Query) -> Parts:
    tab = query.get("tab", "tasks")
    if tab not in dict(TABS):
        raise PartError(f"tab must be one of {', '.join(dict(TABS))}.")
    build = {
        "tasks": lambda: {"groups": groups(snap)},
        "requests": lambda: requests(snap, query),
        "parked": lambda: parked(snap),
        "done": lambda: done_tab.done(snap, query),
        "checks": lambda: checks(snap),
    }
    return {"header": lambda: header(snap), "tabs": lambda: tabs(snap), tab: build[tab]}
