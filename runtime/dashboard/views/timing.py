"""Time: where a task's time goes, end to end, as the warden measured it. Wall time and runtime are told apart and
never added: wall time is calendar time between two moves of a task, runtime the time its agents were doing
something. The page leads with the window's medians; the slowest tasks and the tools' time sit one click away, and
each task's own page tells its timeline beside its runtime."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from runtime.dashboard.views.kit import PartError, Parts, Query, Snap, one_of, plural, span
from runtime.dashboard.views.words import taskwords

WANTS = ("settings", "time")
LISTS = {"slowest": "Slowest tasks", "tools": "Tool time"}
_GROUPS = (
    ("lead", "Designer and implementer", ("model", "tools", "waiting", "idle")),
    ("helpers", "Testers and critics", ("model", "tools", "idle")),
)
_PARTS = {
    "model": ("Model thinking", "--f-design"),
    "tools": ("Tools", "--f-impl"),
    "waiting": ("Waiting on its testers", "--f-qa"),
    "idle": ("Idle, waiting to be woken", "--idle"),
}
_CHECK = {
    "walking": ("A regression tester walking", "--f-qa"),
    "after_failure": ("After a failed check, until the next walk", "--bad"),
    "waiting": ("Waiting for a check to start", "--idle"),
}
WALL_HINT = "Calendar time between two moves of a task, each move counted the first time it was made."
RUN_HINT = (
    "Time an agent was doing something, from its transcript: the model, its tools, waiting on its testers, or idle "
    "until it is woken. Measured only for a task whose every transcript was read from its start."
)


def duration(seconds: float | None) -> str:
    """6 h 36 min, under 1 min, none, or unknown."""
    if seconds is None:
        return "unknown"
    return "none" if not seconds else "under 1 min" if seconds < 60 else span(seconds)


def _pct(part: float, whole: float) -> float:
    return round(part / whole * 100, 1) if whole else 0.0


def _part(key: str, seconds: float, whole: float) -> dict[str, Any]:
    label, color = _PARTS[key]
    return {"key": key, "label": label, "color": color, "value": duration(seconds), "pct": _pct(seconds, whole)}


def _groups(pick: Callable[[str, str], float]) -> list[dict[str, Any]]:
    """Each group's runtime as an alive total and its parts; ``pick(group, part)`` gives the seconds."""
    found = []
    for group, name, keys in _GROUPS:
        whole = sum(pick(group, k) for k in keys)
        parts = [_part(k, pick(group, k), whole) for k in keys]
        found.append({"name": name, "alive": duration(pick(group, "alive")), "parts": parts})
    return found


def wall(snap: Snap) -> dict[str, Any]:
    found = snap.get("time")
    summary = found["summary"]
    top = max((leg["median"] for leg in summary["legs"] if leg["median"] is not None), default=0.0)
    rows = [
        {
            "label": leg["label"],
            "total": leg["key"] == "total",
            "median": duration(leg["median"]) if leg["n"] else "no task yet",
            "note": f"p75 {duration(leg['p75'])}" if leg["n"] else None,
            "pct": _pct(leg["median"], top) if leg["n"] else 0.0,
        }
        for leg in summary["legs"]
    ]
    sub = f"{plural(summary['tasks'], 'task')} reached staging in the last {plural(found['days'], 'day')}"
    return {"sub": sub, "hint": WALL_HINT, "rows": rows, "lists": list(LISTS.items())}


def runtime(snap: Snap) -> dict[str, Any]:
    summary = snap.get("time")["summary"]
    measured = summary["runtime"]
    if not measured["tasks"]:
        empty = "No task here has its runtime measured yet: the warden measures it as the agents work."
        return {"sub": None, "hint": RUN_HINT, "groups": [], "empty": empty}
    sub = f"Median per task, over the {measured['tasks']} of {plural(summary['tasks'], 'task')} measured"
    return {"sub": sub, "hint": RUN_HINT, "groups": _groups(lambda g, p: measured[g][p]["median"]), "empty": None}


def _walk(walks: dict[str, Any]) -> list[str]:
    if not walks["n"]:
        return []
    said = f"A regression walk takes {duration(walks['median'])}, p75 {duration(walks['p75'])}"
    if walks["measured"]:
        said += "; " + ", ".join(f"{_PARTS[p][0].lower()} {duration(walks[p])}" for p in ("model", "tools", "idle"))
    return [said]


def checks(snap: Snap) -> dict[str, Any]:
    summary = snap.get("time")["summary"]
    found = summary["checks"]
    if not found["merges"]:
        return {"sub": "No merge in the window was checked yet.", "kv": [], "split": [], "facts": []}
    wait = found["wait"]
    kv = [("Median", wait["median"]), ("p75", wait["p75"]), ("Longest", wait["max"])]
    merges = plural(found["merges"], "merge")
    facts = [
        f"Checks a merge waited through: {found['checks']:g}, {found['failed']:g} of them failed or blocked (medians)",
        f"{found['held_failure']} of {merges} waited through a failed or blocked check",
    ]
    return {
        "sub": f"{merges} in the window, from merged until its check passed",
        "kv": [{"label": label, "value": duration(seconds)} for label, seconds in kv],
        "split": [_share(key, found[key]) for key in _CHECK],
        "facts": facts + _walk(summary["walks"]),
    }


def _share(key: str, cell: dict[str, float | None]) -> dict[str, Any]:
    """One part of the merges' wait: its share of all of it, and its median per merge."""
    share, (label, color) = cell["share"], _CHECK[key]
    return {
        "key": key,
        "label": label,
        "color": color,
        "value": duration(cell["median"]),
        "share": "unknown" if share is None else f"{round(share * 100)}%",
        "pct": 0.0 if share is None else round(share * 100, 1),
    }


def _slow(task_id: str, task: dict[str, Any], top: float) -> dict[str, Any]:
    run, total = task["runtime"], task["legs"]["total"]
    detail = "runtime unknown"
    if run is not None:
        lead, helpers = duration(run["lead"]["alive"]), duration(run["helpers"]["alive"])
        detail = f"runtime {lead} designer and implementer, {helpers} testers and critics"
    return {"task": task_id, "name": task["title"], "value": duration(total), "pct": _pct(total, top), "detail": detail}


def listing(snap: Snap, which: str) -> dict[str, Any]:
    found = snap.get("time")
    one_of(which, LISTS, "list")
    if which == "tools":
        tools = found["summary"]["tools"]
        top = tools[0][1] if tools else 0.0
        rows = [
            {"task": None, "name": key, "value": duration(seconds), "detail": None, "pct": _pct(seconds, top)}
            for key, seconds in tools
        ]
        note = "Every tool's time over the tasks measured in the window, its agents' and their testers' together."
        return {"title": LISTS[which], "note": note, "rows": rows, "empty": "No task in the window is measured yet."}
    slowest = [(t, found["tasks"][t]) for t in found["summary"]["slowest"]]
    top = slowest[0][1]["legs"]["total"] if slowest else 0.0
    note = "Approved, until on staging: the tasks that reached staging in the window, slowest first."
    rows = [_slow(task_id, task, top) for task_id, task in slowest]
    return {"title": LISTS[which], "note": note, "rows": rows, "empty": "No task reached staging in the window."}


def task_time(snap: Snap, task_id: str) -> dict[str, Any]:
    """A task's own timeline (wall time in each status) beside its runtime."""
    found = snap.get("time")["tasks"].get(task_id)
    if found is None:
        raise PartError(f"The warden sent no time for {task_id}.")
    total, steps, run = found["legs"].get("total"), found["statuses"], found["runtime"]
    top = max((s["seconds"] for s in steps), default=0.0)
    rows = [
        {
            "label": taskwords.word(s["status"]),
            "value": duration(s["seconds"]),
            "pct": _pct(s["seconds"], top),
            "note": "so far" if s["now"] else None,
        }
        for s in steps
    ]
    return {
        "total": "Not on staging yet" if total is None else f"{duration(total)} from approved to on staging",
        "hint": WALL_HINT,
        "rows": rows,
        "runtime": None if run is None else _groups(lambda g, p: run[g][p]),
        "unknown": "Unknown: not every transcript of this task was measured from its start." if run is None else None,
        "run_hint": RUN_HINT,
        "tools": " · ".join(f"{key} {duration(s)}" for key, s in run["tools"]) if run and run["tools"] else None,
    }


def wants(query: Query) -> dict[str, Any]:
    return {"want": list(WANTS)}


def parts(snap: Snap, query: Query) -> Parts:
    if query.get("view") == "list":
        return {"list": lambda: listing(snap, query.get("id", ""))}
    return {"wall": lambda: wall(snap), "runtime": lambda: runtime(snap), "checks": lambda: checks(snap)}
