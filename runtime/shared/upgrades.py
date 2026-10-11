"""An upgrade of the factory, in root's own folder: what is installed, and how an upgrade under way is going.

Only root writes here (the install, and the upgrade service the dashboard starts); the warden reads it for the
dashboard, which keeps showing an upgrade while the warden and the dashboard stop and start again. Root never trusts
the warden's folder for an upgrade: whose laptop it is, and which product checkout, come from the install's own
record here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runtime.shared import store
from runtime.shared.paths import Places
from runtime.shared.releases import FULL_SHA

RUNNING, DONE, FAILED = "running", "done", "failed"


def installed_path(places: Places) -> Path:
    return places.upgrade / "installed.json"


def progress_path(places: Places) -> Path:
    return places.upgrade / "progress.json"


_INSTALLED = ("commit", "owner", "product", "repo")


def read_installed(places: Places) -> dict[str, str] | None:
    """The last finished install: its commit, the owner's login, the owner's product checkout and the factory's
    repo (owner/name); None before one. Raises StoreError when the record cannot be used."""
    raw = store.read_json_or(installed_path(places), None)
    if raw is None:
        return None
    if not isinstance(raw, dict) or not all(isinstance(raw.get(k), str) and raw[k] for k in _INSTALLED):
        raise store.StoreError(f"{installed_path(places)}: needs {', '.join(_INSTALLED)}")
    if not FULL_SHA.match(raw["commit"]):
        raise store.StoreError(f"{installed_path(places)}: the commit is not a full sha")
    return {k: raw[k] for k in _INSTALLED}


def read_progress(places: Places) -> dict[str, Any] | None:
    """The last upgrade's progress: from and to, its state, the step it is at, what stopped it and its log; None
    before the first. Raises StoreError when the record cannot be used."""
    raw = store.read_json_or(progress_path(places), None)
    if raw is None:
        return None
    keys = ("from", "to", "state", "step", "problem", "log", "at")
    if not isinstance(raw, dict) or not all(isinstance(raw.get(k), str) for k in keys):
        raise store.StoreError(f"{progress_path(places)}: needs {', '.join(keys)}")
    if raw["state"] not in (RUNNING, DONE, FAILED):
        raise store.StoreError(f"{progress_path(places)}: the state {raw['state']!r} is not one an upgrade has")
    return {k: raw[k] for k in keys}


def write_progress(places: Places, frm: str, to: str, state: str, step: str, problem: str = "", log: str = "") -> None:
    places.upgrade.mkdir(mode=0o755, parents=True, exist_ok=True)
    at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    record = {"from": frm, "to": to, "state": state, "step": step, "problem": problem, "log": log, "at": at}
    store.write_json(progress_path(places), record, mode=0o644)


def pins_path(places: Places) -> Path:
    return places.upgrade / "pins.json"


def read_pins(places: Places) -> list[str]:
    """The commits an upgrade made to move the product to its factory: the owner's, by asking for the upgrade."""
    raw = store.read_json_or(pins_path(places), [])
    if not isinstance(raw, list) or not all(isinstance(c, str) and FULL_SHA.match(c) for c in raw):
        raise store.StoreError(f"{pins_path(places)}: needs a list of full commits")
    return raw
