"""The top bar and the sidebar every page shares: the repo, staging's and prod's health, the counts, the code lock."""

from __future__ import annotations

from typing import Any

from runtime.dashboard.views.kit import PartError, Parts, Query, Snap, clock
from runtime.shared.models import RETIRED

WANTS = ("settings", "probes", "factory", "sessions", "queue", "reflections")
PAGES = [
    ["home", "Home", "home", "h"],
    ["work", "Work", "queue-list", "w"],
    ["agents", "Agents", "cpu-chip", "a"],
    ["spend", "Spend", "currency-dollar", "s"],
    ["library", "Library", "book-open", "l"],
    ["settings", "Settings", "adjustments-horizontal", "c"],
    ["reflect", "Self-reflect", "light-bulb", "r"],
    ["guard", "Guard", "shield-check", "g"],
]


def health(snap: Snap, env: str) -> dict[str, Any]:
    """An environment's state from its last probe, and the build it last answered on."""
    recent = snap.get("probes")["recent"][env]
    last = recent[-1] if recent else None
    good = next((p for p in reversed(recent) if p["ok"]), None)
    if last is None:
        state = "unknown"
    elif last["ok"]:
        state = "healthy"
    else:
        state = "down" if last["counts"] else "unknown"
    return {"state": state, "sha": good["commit"][:7] if good else None}


def factory_state(snap: Snap) -> str:
    if snap.get("factory")["paused"] is not None:
        return "paused"
    live = [s for s in snap.get("sessions").values() if s["role"] == "orchestrator" and s["state"] != RETIRED]
    return "running" if live else "stopped"


def github_url(snap: Snap) -> str:
    return f"https://github.com/{snap.settings.get_str('repo.github')}"


def _count(snap: Snap, read: Any) -> int | None:
    try:
        return int(read())
    # telemetry: surfaced — the count is left off the sidebar; the page itself draws the failure
    except PartError:
        return None


def shell(snap: Snap) -> dict[str, Any]:
    settings = snap.settings
    until = snap.get("factory")["locked_until"]
    queued = _count(snap, lambda: sum(1 for t in snap.get("queue")["tasks"] if t["status"] == "queued"))
    return {
        "repo": settings.get_str("repo.github").partition("/")[2],
        "github": github_url(snap),
        "pills": {env: health(snap, env) for env in ("staging", "prod")},
        "nav": {"work": queued, "reflect": _count(snap, lambda: len(snap.get("reflections")))},
        "live": {
            "refresh_seconds": settings.get_int("dashboard.refresh_seconds"),
            "stale_seconds": settings.get_int("dashboard.stale_seconds"),
        },
        "factory": factory_state(snap),
        "code": {
            "locked_until": None if until is None else clock(until),
            "lock_minutes": settings.get_int("dashboard.lock_minutes"),
        },
        "pages": PAGES,
    }


def wants(query: Query) -> dict[str, Any]:
    return {"want": list(WANTS)}


def parts(snap: Snap, query: Query) -> Parts:
    return {"shell": lambda: shell(snap)}
