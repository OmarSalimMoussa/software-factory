"""What the warden publishes for other users to read, in its state folder's public/ (0755).

The guard and the sessions read the effective settings, the rule switches, the machine's vitals and who holds each
slot's desktop here; the warden alone writes them.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from runtime.shared import store
from runtime.shared.paths import Places
from runtime.shared.settings import Settings


def folder(places: Places) -> Path:
    return places.state / "public"


def write_settings(places: Places, settings: Settings) -> None:
    """The effective settings tree, exactly as the warden reads it."""
    store.write_json(folder(places) / "settings.json", settings.tree())


def write_switches(places: Places, off: Iterable[str]) -> None:
    """The rules the owner has switched off, in the warden's state and in public/ for the guard."""
    record = {"off": sorted(set(off))}
    store.write_json(places.switches, record, mode=0o600)
    store.write_json(folder(places) / "switches.json", record)


def write_vitals(places: Places, level: str, why: str, cpu: float | None, at: str) -> None:
    """The machine's vitals level, the readings that set it and its average CPU use, for the starts of walks."""
    store.write_json(folder(places) / "vitals.json", {"level": level, "why": why, "cpu": cpu, "at": at})


def desktops_path(places: Places) -> Path:
    """Who holds each slot's desktop (slot number to subagent id), for the guard's desktop time."""
    return folder(places) / "desktops.json"


def staging_at_path(places: Places) -> Path:
    """The commit slot 0 builds from its own worktree (session_settings.staging_at), for the stack runner, while a
    check walks an older commit than the tip; none while it builds the main checkout."""
    return folder(places) / "staging-at.json"


def read_switches(places: Places) -> set[str]:
    raw = store.read_json_or(places.switches, {"off": []})
    off = raw.get("off") if isinstance(raw, dict) else None
    if not isinstance(off, list) or not all(isinstance(rule, str) for rule in off):
        raise store.StoreError(f"{places.switches}: needs an 'off' list of rule ids")
    return set(off)
