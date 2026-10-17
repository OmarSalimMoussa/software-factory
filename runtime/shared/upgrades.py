"""An upgrade of the factory, in root's own folder: what is installed, how an upgrade under way is going, and the
commits root made on the agents' default branch for the owner.

Only root writes here (the install, and the upgrade unit the warden starts); the warden reads it for the dashboard,
which keeps showing an upgrade while the warden and the dashboard stop and start again. For an upgrade nobody
watches, root takes whose machine it is, the product checkout, the factory repo and the token from here.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runtime.shared import store
from runtime.shared.paths import Places
from runtime.shared.settings import Settings, build
from standard.config import FACTORY_REF

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


def pins_path(places: Places) -> Path:
    return places.upgrade / "pins.json"


def owned_path(places: Places) -> Path:
    """The owner's own changes to paths only a person changes, which root committed on the owner's command."""
    return places.upgrade / "owned.json"


# Workflows the factory once copied into products, and copies no more.
RETIRED_WORKFLOWS = ("heartbeat.yml",)


def pin_files(templates: Path, repo: str, commit: str) -> dict[str, str | None]:
    """What moving a product to the factory at ``commit`` sets: each path's text, or None for one it removes."""
    found: dict[str, str | None] = {FACTORY_REF: f"{repo}@{commit}\n"}
    found |= {f".github/workflows/{p.name}": p.read_text(encoding="utf-8") for p in sorted(templates.glob("*.yml"))}
    return found | {f".github/workflows/{name}": None for name in RETIRED_WORKFLOWS}


def blob_id(text: str) -> str:
    """The id git gives a file of ``text``, worked out here: root does not take git's word, run as the agent, for it."""
    data = text.encode()
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def _record_ok(record: object) -> bool:
    if not isinstance(record, dict) or set(record) != {"commit", "parent", "blobs"}:
        return False
    shas = (
        isinstance(record["commit"], str)
        and bool(FULL_SHA.match(record["commit"]))
        and isinstance(record["parent"], str)
    )
    return shas and isinstance(record["blobs"], dict)


def _records(path: Path) -> list[dict[str, Any]]:
    raw = store.read_json_or(path, [])
    if not isinstance(raw, list) or not all(_record_ok(r) for r in raw):
        raise store.StoreError(f"{path}: needs a list of commits, parents and blobs")
    found: list[dict[str, Any]] = raw
    return found


def read_pins(places: Places) -> list[dict[str, Any]]:
    """Each commit an upgrade made to move the product to its factory, its parent, and each path's blob (or None)."""
    return _records(pins_path(places))


def read_owned(places: Places) -> list[dict[str, Any]]:
    """Each commit root made of the owner's own change, its parent, and each path's blob (or None)."""
    return _records(owned_path(places))
