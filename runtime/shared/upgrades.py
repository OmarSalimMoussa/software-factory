"""An upgrade of the factory, in root's own folder: what is installed, and how an upgrade under way is going.

Only root writes here (the install, and the upgrade unit the warden starts); the warden reads it for the dashboard,
which keeps showing an upgrade while the warden and the dashboard stop and start again. For an upgrade nobody
watches, root takes whose machine it is, the product checkout, the factory repo and the token from here.
"""

from __future__ import annotations

import re
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runtime.shared import store
from runtime.shared.paths import Places
from runtime.shared.settings import Settings, build

RUNNING, DONE, FAILED = "running", "done", "failed"
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


def installed_path(places: Places) -> Path:
    return places.upgrade / "installed.json"


def settings_path(places: Places) -> Path:
    """The saved settings the last install applied, which an unattended upgrade applies as they were."""
    return places.upgrade / "settings.json"


def read_settings(places: Places) -> dict[str, Any] | None:
    raw = store.read_json_or(settings_path(places), None)
    if raw is not None and not isinstance(raw, dict):
        raise store.StoreError(f"{settings_path(places)}: not an object of settings")
    return raw


def installed_settings(places: Places) -> Settings:
    record = read_settings(places)
    if record is None:
        raise store.StoreError(f"{settings_path(places)} is missing: install again")
    return build(tomllib.loads(places.defaults.read_text(encoding="utf-8")), record)


def token_path(places: Places) -> Path:
    """Root's own copy of the read-only factory token, which the upgrade unit reads instead of the warden's."""
    return places.upgrade / "factory.token"


def progress_path(places: Places) -> Path:
    return places.upgrade / "progress.json"


_INSTALLED = ("commit", "owner", "product", "repo")


def read_installed(places: Places) -> dict[str, str] | None:
    """The last finished install's commit, owner, product checkout and factory repo; None before one."""
    raw = store.read_json_or(installed_path(places), None)
    if raw is None:
        return None
    if not isinstance(raw, dict) or not all(isinstance(raw.get(k), str) and raw[k] for k in _INSTALLED):
        raise store.StoreError(f"{installed_path(places)}: needs {', '.join(_INSTALLED)}")
    if not FULL_SHA.match(raw["commit"]):
        raise store.StoreError(f"{installed_path(places)}: the commit is not a full sha")
    return {k: raw[k] for k in _INSTALLED}


def read_progress(places: Places) -> dict[str, Any] | None:
    """The last upgrade's from and to, state, step, what stopped it and its log; None before the first."""
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
