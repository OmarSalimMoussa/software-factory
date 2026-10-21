"""Where a task's time went, told two ways that are never added together. Wall time is the calendar time between two
moves in its log, each move counted the first time it was made. Runtime is the time its agents were doing something,
from their transcripts (partition.py): its lead (designer and implementer) apart from their testers and critics, and
unknown unless every transcript of it was measured whole.

A merge's wait for its check splits into the regression testers walking (any walk, since a check covers every merge
waiting), the time after a failed or blocked check until the next walk starts, and waiting for a check otherwise. The
summary covers the tasks that reached staging in the last ``dashboard.time_days`` days. All times are in seconds.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from typing import Any

from runtime.qa.carry import PASS
from runtime.queue.book import Queue
from runtime.queue.formats import epoch
from runtime.shared.models import MERGED, ON_STAGING, QA_CLEAN, RETIRED
from runtime.warden.context import Context
from runtime.warden.folds import Times, kept
from runtime.warden.sessions import registry
from runtime.warden.verbs import REGRESSION

WORK = ("designing", "building")
LEGS: tuple[tuple[str, str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("total", "Approved, until on staging", ("approved",), (ON_STAGING,)),
    ("queued", "Queued, until proposed", ("queued",), ("proposed",)),
    ("approval", "Proposed, until approved", ("proposed",), ("approved",)),
    ("start", "Approved, until work starts", ("approved",), WORK),
    ("build", "Design and build, until its first QA", WORK, ("qa",)),
    ("qa", "QA, until it passes", ("qa",), (QA_CLEAN,)),
    ("turn", "Passed, until its merge turn", (QA_CLEAN,), ("turn",)),
    ("merge", "Merging", ("turn",), (MERGED,)),
    ("check", "Merged, until its check passes", (MERGED,), ("checked",)),
    ("staging", "Checked, until on staging", ("checked",), (ON_STAGING,)),
    ("prod", "On staging, until on prod", (ON_STAGING,), ("promoted",)),
)
_DONE = ("promoted", "closed")  # a timeline stops counting once its task is on prod or closed
_RUNTIME, _PARTS = ("model", "tools", "waiting", "idle", "alive"), ("walking", "waiting", "after_failure")
Span = tuple[float, float]


def _moves(log: list[dict[str, str]]) -> list[tuple[str, float]]:
    return [(e["to"], epoch(e["at"])) for e in log if e.get("to")]


def _legs(first: dict[str, float]) -> dict[str, float]:
    found = {}
    for key, _, starts, ends in LEGS:
        start = min((first[s] for s in starts if s in first), default=None)
        end = min((first[s] for s in ends if s in first), default=None)
        if start is not None and end is not None and end >= start:
            found[key] = end - start
    return found


def _statuses(moves: list[tuple[str, float]], now: float) -> list[dict[str, Any]]:
    spent: dict[str, float] = {}
    for (status, at), (_, then) in zip(moves, [*moves[1:], ("", now)], strict=True):
        if status not in _DONE:
            spent[status] = spent.get(status, 0.0) + then - at
    current = moves[-1][0] if moves else ""
    return [{"status": status, "seconds": seconds, "now": status == current} for status, seconds in spent.items()]


def _parts(agents: list[dict[str, Any]]) -> dict[str, float]:
    found = {part: sum(a[part] for a in agents) for part in ("model", "waiting", "idle")}
    found["tools"] = sum(sum(a["tools"].values()) for a in agents)
    return {part: found[part] if part != "alive" else sum(found.values()) for part in _RUNTIME}


def _ranked(agents: list[dict[str, Any]]) -> list[list[Any]]:
    tools: dict[str, float] = {}
    for agent in agents:
        for key, seconds in agent["tools"].items():
            tools[key] = tools.get(key, 0.0) + seconds
    return [[key, seconds] for key, seconds in sorted(tools.items(), key=lambda kv: -kv[1])]


def _runtime(agents: list[dict[str, Any]]) -> dict[str, Any] | None:
    lead = [a for a in agents if a["lead"]]
    if not lead or not all(a["whole"] for a in agents):
        return None
    return {"lead": _parts(lead), "helpers": _parts([a for a in agents if not a["lead"]]), "tools": _ranked(agents)}


def _check_wait(merged: float, checked: float, walks: list[Span], checks: list[tuple[float, bool]]) -> dict[str, Any]:
    # Every regression walk ever made is given, and each piece of the wait is weighed against the walks: only those
    # that overlap the wait can touch it, and weighing them all grows with every walk.
    walks = [(s, e) for s, e in walks if s < checked and e > merged]
    inside = [(at, ok) for at, ok in checks if merged < at <= checked]
    starts = [(s, True) for s, _ in walks if merged < s < checked]  # a walk that starts ends the wait after a failure
    cuts = sorted({merged, checked, *(t for w in walks for t in w if merged < t < checked), *(at for at, _ in inside)})
    events = sorted(inside + starts)
    found = dict.fromkeys(_PARTS, 0.0)
    for a, b in zip(cuts, cuts[1:], strict=False):
        if any(s <= a and b <= e for s, e in walks):
            found["walking"] += b - a
            continue
        last = [ok for at, ok in events if at <= a]
        found["after_failure" if last and not last[-1] else "waiting"] += b - a
    return {**found, "checks": len(inside), "failed": sum(1 for _, ok in inside if not ok)}


def _stats(values: Sequence[float]) -> dict[str, float | None]:
    if len(values) < 2:
        return {"median": values[0] if values else None, "p75": values[0] if values else None}
    return {"median": statistics.median(values), "p75": statistics.quantiles(values, n=4, method="inclusive")[2]}


def _median(values: Sequence[float]) -> float | None:
    return statistics.median(values) if values else None


def read(ctx: Context, wanted: list[str]) -> dict[str, Any]:
    """Each task in the window and each one ``wanted``, told in wall time and runtime, and the window's summary."""
    days, now = ctx.settings().get_int("dashboard.time_days"), ctx.clock.now()
    since, queue = now - days * 86400, Queue.of(ctx)
    times = kept(Times, ctx.places.times)
    times.read(ctx.places.times)
    agents: dict[str, list[dict[str, Any]]] = {}
    for agent in times.agents.values():
        agents.setdefault(agent["task"], []).append(agent)
    rows = registry.read(ctx.places).values()
    testers = [s for s in rows if s.role == REGRESSION and (s.ended or s.state != RETIRED)]
    walks = [(epoch(s.started), epoch(s.ended) if s.ended else now) for s in testers]
    checks = [(epoch(c.at), all(r.result == PASS for r in c.rows)) for c in queue.checks()[0]]
    tasks: dict[str, dict[str, Any]] = {}
    window, merges = [], []
    for task in queue.tasks()[0]:
        moves = _moves(queue.read_log(task.id))
        first = dict(reversed(moves))
        merged, checked = first.get(MERGED), first.get("checked")
        check = None
        if merged is not None and checked is not None and checked >= merged:
            check = _check_wait(merged, checked, walks, checks)
            merges += [check] if merged >= since else []
        tasks[task.id] = {"title": task.title, "legs": _legs(first), "statuses": _statuses(moves, now),
                          "runtime": _runtime(agents.get(task.id, [])), "check": check}  # fmt: skip
        window += [task.id] if since <= first.get(ON_STAGING, 0) <= now else []
    walked = [(times.agents.get(s.agent_id), epoch(s.ended) - epoch(s.started))
              for s in testers if s.ended and epoch(s.started) >= since]  # fmt: skip
    known = [tasks[t]["runtime"] for t in window if tasks[t]["runtime"] is not None]
    summary = {
        "tasks": len(window),
        "legs": _leg_stats([tasks[t]["legs"] for t in window]),
        "runtime": {
            "tasks": len(known),
            **{g: {p: _stats([r[g][p] for r in known]) for p in _RUNTIME} for g in ("lead", "helpers")},
        },
        "tools": _ranked([{"tools": dict(r["tools"])} for r in known]),
        "checks": _check_stats(merges),
        "walks": _walk_stats(walked),
        "slowest": sorted((t for t in window if "total" in tasks[t]["legs"]), key=lambda t: -tasks[t]["legs"]["total"]),
    }
    return {"days": days, "tasks": {t: tasks[t] for t in dict.fromkeys([*window, *wanted]) if t in tasks},
            "summary": summary}  # fmt: skip


def _leg_stats(legs: list[dict[str, float]]) -> list[dict[str, Any]]:
    found = []
    for key, label, _, _ in LEGS:
        values = [leg[key] for leg in legs if key in leg]
        found.append({"key": key, "label": label, "n": len(values), **_stats(values)})
    return found


def _check_stats(merges: list[dict[str, Any]]) -> dict[str, Any]:
    waits = [sum(m[p] for p in _PARTS) for m in merges]
    total = sum(waits)
    return {
        "merges": len(merges),
        "wait": {**_stats(waits), "max": max(waits, default=None)},
        **{p: {"median": _median([m[p] for m in merges]), "share": sum(m[p] for m in merges) / total if total else None}
           for p in _PARTS},
        "checks": _median([m["checks"] for m in merges]),
        "failed": _median([m["failed"] for m in merges]),
        "held_failure": sum(1 for m in merges if m["failed"]),
    }  # fmt: skip


def _walk_stats(walked: list[tuple[dict[str, Any] | None, float]]) -> dict[str, Any]:
    """The regression testers' walks: how long each took, and the medians of what it took it, where measured whole."""
    measured = [_parts([agent]) for agent, _ in walked if agent is not None and agent["whole"]]
    parts = {p: _median([m[p] for m in measured]) for p in ("model", "tools", "idle")}
    return {"n": len(walked), **_stats([seconds for _, seconds in walked]), "measured": len(measured), **parts}
