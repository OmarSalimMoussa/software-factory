"""Push to prod: prod's build beside the one that ships, what prod gains with what each change was built from, and
what is held back for a later push, each with why. The warden's push plan says which build ships."""

from __future__ import annotations

from typing import Any

from runtime.dashboard.views.home import env_card
from runtime.dashboard.views.kit import Parts, Query, Snap, epoch, span, when
from runtime.dashboard.views.shell import github_url, health, prod_runs_staging
from runtime.dashboard.views.tasks.task import built_from
from runtime.dashboard.views.words import taskwords
from runtime.shared.models import ON_STAGING

WANTS = ("settings", "probes", "queue", "deploys", "sessions", "cost", "cards", "qa", "specs", "rules", "outage")


def _prod(snap: Snap) -> dict[str, Any]:
    card = env_card(snap, "prod")
    current = snap.get("deploys")["prod"]["current"]
    return {
        "sha": card["sha"],
        "deployed": when(epoch(current["at"]), utc=True) if current else None,
        "commit_url": card["commit_url"],
    }


def _ships(snap: Snap, ships: dict[str, Any]) -> dict[str, Any]:
    latest = snap.get("probes")["recent"]["staging"]
    streak = snap.get("probes")["streak"].get("staging")
    live = bool(latest) and latest[-1]["ok"] and latest[-1]["commit"] == ships["commit"]
    healthy = live and health(snap, "staging")["state"] == "healthy" and streak is not None and streak["ok"]
    return {
        "sha": ships["full"][:7],
        "deployed": when(epoch(ships["at"]), utc=True),
        "healthy_for": span(snap.now - epoch(streak["since"])) if healthy and streak else None,
        "commit_url": f"{github_url(snap)}/commit/{ships['full']}",
    }


def _gain(snap: Snap, tasks: dict[str, dict[str, Any]], task_id: str) -> dict[str, Any]:
    task = tasks.get(task_id)
    if task is None:
        return {"id": task_id, "title": task_id, "chip": {"text": "no task file", "tone": "bad"}, "built_from": None}
    specs = snap.get("specs")
    chip = (
        {"text": "spec", "tone": "violet"}
        if task_id in specs and specs[task_id]
        else {"text": f"rung {taskwords.rung(task, snap.settings)}", "tone": "info"}
    )
    return {"id": task_id, "title": task["title"], "chip": chip, "built_from": built_from(snap, task)}


def _held(tasks: dict[str, dict[str, Any]], row: dict[str, Any]) -> dict[str, Any]:
    task = row["task"]
    if task is None:
        return {"id": None, "title": f"merge {row['merge'][:12]}", "why": row["why"]}
    return {"id": task, "title": tasks[task]["title"] if task in tasks else task, "why": row["why"]}


def push(snap: Snap) -> dict[str, Any]:
    plan = snap.get("deploys")["plan"]
    ships = plan["ships"] if plan else None
    tasks = taskwords.tasks(snap)
    gains = [_gain(snap, tasks, task_id) for task_id in plan["gains"]] if plan else []
    held = [_held(tasks, row) for row in plan["held"]] if plan else []
    previous = snap.get("deploys")["push_back"]
    return {
        "prod": _prod(snap),
        "ships": None if ships is None else _ships(snap, ships),
        "tasks": gains,
        "gains": len(gains),
        "held": held,
        "held_count": len(held),
        "prod_runs_staging": prod_runs_staging(snap),
        "previous_prod": previous[:7] if previous else None,
        "ships_sha": None if ships is None else ships["full"],
        "blocked": snap.get("deploys")["push_refused"],
    }


def wants(query: Query) -> dict[str, Any]:
    return {"want": list(WANTS), "statuses": ["checked", ON_STAGING], "tasks": []}


def parts(snap: Snap, query: Query) -> Parts:
    return {"push": lambda: push(snap)}
