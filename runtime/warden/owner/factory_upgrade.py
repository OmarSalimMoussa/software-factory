"""An upgrade of the factory to any commit of its repo on GitHub, started with the owner's factory.upgrade.

The verb takes the commit's full sha. It pauses the factory, waits for every agent to settle, and starts root's
upgrade unit, which downloads that commit itself and installs it. No commit need be newer than the one installed, so
installing the commit before is the way back. The warden stops while the install runs: what was asked for stays here
for the page, and root's progress record says how it went.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any

from runtime.shared import operations, store, upgrades
from runtime.shared.models import RESTING
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused, ok
from runtime.warden.context import Context
from runtime.warden.owner import ops
from runtime.warden.sessions import lifecycle, registry, revive


def asked_path(places: Places) -> Path:
    return places.state / "upgrade-asked.json"


def asked(places: Places) -> dict[str, Any] | None:
    """The upgrade the owner asked for last: from and to, when, and the operation that started it."""
    raw = store.read_json_or(asked_path(places), None)
    if raw is not None and (not isinstance(raw, dict) or set(raw) != {"from", "to", "at", "op"}):
        raise store.StoreError(f"{asked_path(places)}: needs from, to, at and op")
    return raw


def start(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    places = ctx.places
    commit = args.get("commit")
    if not isinstance(commit, str) or not upgrades.FULL_SHA.match(commit):
        raise Refused("upgrade", "Name the factory's commit by its full 40-character sha")
    with ctx.lock:
        progress, before = progress_now(ctx), asked(places)
        if progress is not None and progress["state"] == upgrades.RUNNING:
            raise Refused("upgrading", "An upgrade is already under way")
        getting_ready = _op(ctx, before["op"]) if before else None
        if getting_ready is not None and getting_ready["state"] in (ops.WAITING, ops.RUNNING):
            raise Refused("upgrading", "An upgrade is already getting ready")
        installed = upgrades.read_installed(places)
        if installed is None:
            raise Refused("upgrade", "Install the factory by hand first")
        steps = [
            ("Pausing the factory", lambda: _pause(ctx)),
            ("Waiting for every agent to settle", lambda: _settled(ctx)),
            ("Starting the upgrade", lambda: _start(ctx, commit)),
        ]
        op_id = ops.launch(ctx, operations.UPGRADE, steps, {"to": commit})
        record = {"from": installed["commit"], "to": commit, "op": op_id, "at": ctx.clock.iso()}
        store.write_json(asked_path(places), record, mode=0o600)
    return ok(op=op_id)


def progress_now(ctx: Context) -> dict[str, Any] | None:
    """Root's progress, an upgrade quiet for longer than one may run read as stopped: a reboot ends one silently."""
    found, before = upgrades.read_progress(ctx.places), asked(ctx.places)
    limit = ctx.settings().get_int("install.upgrade_minutes") * 60
    quiet = "The upgrade went quiet and did not finish. Its log in /var/log/software-factory says more; upgrade again."
    if before is not None and (found is None or found["to"] != before["to"] or found["at"] < before["at"]):
        op = _op(ctx, before["op"])
        if op is not None and op["state"] == ops.DONE and ctx.clock.now() - _epoch(before["at"]) > limit:
            stopped = {"state": upgrades.FAILED, "step": "", "problem": quiet, "log": ""}
            return {"from": before["from"], "to": before["to"], "at": before["at"], **stopped}
        return found
    if found is not None and found["state"] == upgrades.RUNNING and ctx.clock.now() - _epoch(found["at"]) > limit:
        return {**found, "state": upgrades.FAILED, "problem": quiet}
    return found


def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


def _op(ctx: Context, op_id: str) -> dict[str, Any] | None:
    """The operation that started an upgrade; None once the nightly clean-up took its record."""
    try:
        found: dict[str, Any] = ops.get(ctx, {"id": op_id}, None)["op"]
    # telemetry: expected — an old operation's record is cleaned up; the upgrade it started is over
    except Refused:
        return None
    return found


def _pause(ctx: Context) -> str:
    if lifecycle.paused_path(ctx).exists():
        return "it was paused already"
    revive.owner_pause(ctx, {}, None)
    return "paused, every agent keeping its work"


def _settled(ctx: Context) -> str:
    """Every agent benched or ended: the install refuses while any works, waits, or is between the two."""
    settings = ctx.settings()
    wait = settings.get_int("install.upgrade_wait_minutes")
    deadline = ctx.clock.now() + wait * 60
    while True:
        busy = sorted(s.name for s in registry.read(ctx.places).values() if s.state not in RESTING)
        if not busy:
            return "every agent has settled"
        if ctx.clock.now() >= deadline:
            raise ops.StepError(f"{', '.join(busy)} did not settle within {wait} minutes: resume, then upgrade again")
        time.sleep(settings.get_int("watch.tick_seconds"))


def _start(ctx: Context, commit: str) -> str:
    ctx.launcher.start_upgrade(commit)
    return "started; the dashboard restarts while the factory is installed"


def _asked_for_installed(places: Places, installed: dict[str, Any] | None) -> dict[str, Any] | None:
    """The upgrade asked for from the factory installed now, or to it: none once an install by hand replaced both."""
    before = asked(places)
    if before is None or installed is None or installed["commit"] not in (before["from"], before["to"]):
        return None
    return before


def view(ctx: Context) -> dict[str, Any]:
    places = ctx.places
    installed = upgrades.read_installed(places)
    before = _asked_for_installed(places, installed)
    return {
        "installed": installed["commit"] if installed else None,
        "asked": before,
        "op": _op(ctx, before["op"]) if before else None,
        "progress": progress_now(ctx) if before else None,
        "paused": lifecycle.paused_path(ctx).exists(),
    }
