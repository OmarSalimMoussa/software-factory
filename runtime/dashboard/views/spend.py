"""Spend: the plan's own windows first, then dollars at API prices against the daily budget, by day and family,
and sliced by family, agent, task (start to merge) and model."""

from __future__ import annotations

import math
from typing import Any

from runtime.dashboard.views import agentwords, taskwords
from runtime.dashboard.views.kit import (
    FAMILIES,
    PartError,
    Parts,
    Query,
    Snap,
    ago,
    day_label,
    iso,
    model_label,
    page,
    plural,
    span,
    when,
)

WANTS = ("settings", "plan", "cost", "queue", "sessions")
RANGES = {"1": "Today", "7": "Last 7 days", "14": "Last 14 days"}
SLICES = [["family", "By family"], ["agent", "By agent"], ["task", "By task, start to merge"], ["model", "By model"]]
_MODEL_COLORS = ("--f-design", "--f-impl", "--f-qa", "--f-orch", "--f-intake")
_WINDOWS = (("five_hour", "5-hour window"), ("seven_day", "This week"))


def _days(snap: Snap, query: Query) -> list[str]:
    raw = query.get("range", "14")
    if raw not in RANGES:
        raise PartError(f"range must be one of {', '.join(RANGES)}.")
    return [iso(snap.now - n * 86400)[:10] for n in range(int(raw) - 1, -1, -1)]


def _cells(snap: Snap, days: list[str], slice_: str) -> dict[str, list[float]]:
    found: dict[str, list[float]] = {}
    for day in days:
        for key, (usd, unknown) in snap.get("cost")["days"].get(day, {}).get(slice_, {}).items():
            cell = found.setdefault(key, [0.0, 0])
            cell[0] += usd
            cell[1] += unknown
    return found


def plan(snap: Snap) -> dict[str, Any]:
    reading = snap.get("plan")
    settings = snap.settings
    if reading["state"] != "fresh":
        note = "No reading yet" if reading["state"] == "missing" else f"No reading for {span(reading['age_seconds'])}"
        age = "no reading yet" if reading["state"] == "missing" else "reading too old to trust"
        return {"stale": True, "age": age, "windows": [{"name": n, "pct": None, "note": note} for _, n in _WINDOWS]}
    amber, red = settings.get_int("cost.plan_amber_percent"), settings.get_int("cost.plan_red_percent")
    windows = []
    for key, name in _WINDOWS:
        window = reading[key]
        if window is None:
            windows.append({"name": name, "pct": None, "note": "The plan did not report this window"})
            continue
        pct, resets = window["used_percentage"], window["resets_at"]
        tone = "bad" if pct >= red else "warn" if pct >= amber else ""
        windows.append(
            {
                "name": name,
                "pct": pct,
                "tone": tone,
                "resets": f"resets in {span(resets - snap.now)} · {when(resets, utc=True)}",
            }
        )
    return {"stale": False, "age": f"read {ago(snap.now - reading['age_seconds'], snap.now)}", "windows": windows}


def _merge_cost(snap: Snap) -> str:
    landed = [t for t in taskwords.tasks(snap).values() if t["status"] in taskwords.LANDED]
    known = [c[0] for t in landed if (c := taskwords.cost_cell(snap, t)) is not None and not c[1]]
    return f"${sum(known) / len(known):,.0f}" if known else "no merges yet"


def hero(snap: Snap, query: Query) -> dict[str, Any]:
    days = _days(snap, query)
    family = _cells(snap, days, "family")
    total, unknown = sum(c[0] for c in family.values()), int(sum(c[1] for c in family.values()))
    cells = _cells(snap, days[-1:], "family").values()
    today, today_unknown = sum(c[0] for c in cells), int(sum(c[1] for c in cells))
    budget = snap.settings.get_float("cost.daily_budget_usd")
    # With no price known for any of today's calls, today's spend is unknown, never $0.
    known = not (today_unknown and not today)
    return {
        "label": RANGES[str(len(days))],
        "total": None if unknown and not total else round(total, 2),
        "budget_pct": round(today / budget * 100) if known else None,
        "today": f"today {'≥ ' if today_unknown else ''}${today:,.0f} of ${budget:,.0f}, at API prices"
        if known
        else f"today unknown of ${budget:,.0f}: no price is set for its models",
        "kv": [
            {"label": "Average cost of a merge", "value": _merge_cost(snap)},
            {"label": "Per day, average", "value": f"${total / len(days):,.0f}"},
        ],
        "unknown": f"{plural(unknown, 'call')} with unknown price" if unknown else None,
    }


def _scale(top: float) -> tuple[float, list[float]]:
    """A chart's top and its three ticks: 0, a round step, twice the step."""
    half = max(top, 1.0) * 1.1 / 2
    power = 10 ** math.floor(math.log10(half))
    step = next(m * power for m in (1, 2, 2.5, 5, 10) if m * power >= half)
    return 2 * step, [0, step, 2 * step]


def chart(snap: Snap, query: Query) -> dict[str, Any]:
    days = _days(snap, query)
    budget = snap.settings.get_float("cost.daily_budget_usd")
    rows = []
    for n, day in enumerate(days):
        cells = snap.get("cost")["days"].get(day, {}).get("family", {})
        values = [round(cells.get(key, [0.0, 0])[0], 2) for key in FAMILIES]
        label = (
            "today"
            if day == days[-1]
            else day_label(day)
            if (len(days) - 1 - n) % (2 if len(days) > 7 else 1) == 0
            else None
        )
        rows.append({"label": label, "values": values, "total": round(sum(values), 2)})
    top, ticks = _scale(max([r["total"] for r in rows] + [budget]))
    return {
        "families": [{"key": k, "label": label, "color": color} for k, (label, color) in FAMILIES.items()],
        "days": rows,
        "max": top,
        "ticks": ticks,
        "budget": budget,
    }


def _value(cell: list[float]) -> tuple[float | None, dict[str, str] | None]:
    usd, unknown = cell
    tag = {"text": f"{plural(int(unknown), 'call')} of unknown price", "tone": "warn"} if unknown else None
    return (None if unknown and not usd else round(usd, 2)), tag


def slice_rows(snap: Snap, query: Query) -> dict[str, Any]:
    key = query.get("slice", "family")
    days = _days(snap, query)
    rows: list[dict[str, Any]] = []
    if key == "family":
        for fam, cell in _cells(snap, days, "family").items():
            label, color = FAMILIES.get(fam, (fam, "--idle"))
            value, tag = _value(cell)
            rows.append({"name": label, "value": value, "color": color, "tag": tag})
    elif key == "agent":
        sessions = snap.get("sessions")
        by_agent = {r["agent_id"] or r["id"]: r for r in sessions.values()}
        for agent, cell in _cells(snap, days, "agent").items():
            row = by_agent.get(agent)
            name = f"{agentwords.name(snap, row)} · {agentwords.role(row)}" if row else "An agent the factory forgot"
            value, tag = _value(cell)
            rows.append(
                {"name": name, "value": value, "color": FAMILIES[row["family"]][1] if row else "--idle", "tag": tag}
            )
    elif key == "task":
        for task in taskwords.tasks(snap).values():
            spent = taskwords.cost_cell(snap, task)
            if spent is None:
                continue
            merged = task["status"] in taskwords.LANDED
            value, tag = _value(spent)
            rows.append(
                {
                    "name": task["title"],
                    "value": value,
                    "color": "--good" if merged else "--accent",
                    "tag": tag
                    or ({"text": "merged", "tone": "good"} if merged else {"text": "in progress", "tone": ""}),
                }
            )
    elif key == "model":
        allowed = snap.settings.get_list("models.allowed")
        for model, cell in _cells(snap, days, "model").items():
            value, tag = _value(cell)
            color = _MODEL_COLORS[allowed.index(model) % len(_MODEL_COLORS)] if model in allowed else "--idle"
            rows.append(
                {
                    "name": model_label(model) if model else "A model the transcript did not name",
                    "value": value,
                    "color": color,
                    "tag": tag,
                }
            )
    else:
        raise PartError(f"slice must be one of {', '.join(k for k, _ in SLICES)}.")
    rows.sort(key=lambda r: -1.0 if r["value"] is None else r["value"], reverse=True)
    chunk, more = page(rows, query, snap.settings.get_int("dashboard.page_size"))
    return {"tabs": SLICES, "rows": chunk, "more": more}


def wants(query: Query) -> dict[str, Any]:
    raw = query.get("range", "14")
    return {"want": list(WANTS), "days": int(raw) if raw in RANGES else 14}


def parts(snap: Snap, query: Query) -> Parts:
    return {
        "plan": lambda: plan(snap),
        "hero": lambda: hero(snap, query),
        "chart": lambda: chart(snap, query),
        "slice": lambda: slice_rows(snap, query),
    }
