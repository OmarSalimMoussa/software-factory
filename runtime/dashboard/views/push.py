"""Push to prod: both builds side by side, and what prod gains, with what each change was built from."""

from __future__ import annotations

from typing import Any

from runtime.dashboard.views.home import env_card
from runtime.dashboard.views.kit import Parts, Query, Snap, epoch, span, when
from runtime.dashboard.views.shell import health, prod_runs_staging
from runtime.dashboard.views.task import built_from
from runtime.dashboard.views.words import taskwords

WANTS = ("settings", "probes", "queue", "deploys", "ci", "sessions", "cost", "cards", "qa", "specs", "rules", "outage")


def _build(snap: Snap, env: str) -> dict[str, Any]:
    card = env_card(snap, env)
    current = snap.get("deploys")[env]["current"]
    return {
        "sha": card["sha"],
        "deployed": when(epoch(current["at"]), utc=True) if current else None,
        "run_url": card["deployment_url"],
        "commit_url": card["commit_url"],
    }


def push(snap: Snap) -> dict[str, Any]:
    staging = _build(snap, "staging")
    pill = health(snap, "staging")
    streak = snap.get("probes")["streak"].get("staging")
    healthy = pill["state"] == "healthy" and streak is not None and streak["ok"]
    staging["healthy_for"] = span(snap.now - epoch(streak["since"])) if healthy and streak else None
    latest = snap.get("probes")["recent"]["staging"]
    expected = latest[-1]["commit"] if latest and latest[-1]["ok"] else None
    same = prod_runs_staging(snap)
    previous = snap.get("deploys")["push_back"]
    tasks = []
    specs = snap.get("specs")
    for task in sorted(taskwords.tasks(snap).values(), key=lambda t: t["created"]):
        if task["status"] != "on-staging":
            continue
        n = taskwords.rung(task, snap.settings)
        chip = (
            {"text": "spec", "tone": "violet"}
            if task["id"] in specs and specs[task["id"]]
            else {"text": f"rung {n}", "tone": "info"}
        )
        tasks.append({"id": task["id"], "title": task["title"], "chip": chip, "built_from": built_from(snap, task)})
    blocked = {
        "down": "Staging is red: the push waits until it is healthy.",
        "unknown": "Staging's health is unknown: the push waits for its next probe.",
    }.get(pill["state"])
    if blocked is None:
        blocked = snap.get("deploys")["staging_pinned"]
    return {
        "prod": _build(snap, "prod"),
        "staging": staging,
        "tasks": tasks,
        "prod_runs_staging": same,
        "previous_prod": previous[:7] if previous else None,
        "expected_staging_sha": expected,
        "blocked": blocked,
    }


def wants(query: Query) -> dict[str, Any]:
    return {"want": list(WANTS), "statuses": ["on-staging"], "tasks": []}


def parts(snap: Snap, query: Query) -> Parts:
    return {"push": lambda: push(snap)}
