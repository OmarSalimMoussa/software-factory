"""The owner's Upgrade from the dashboard: a newer factory found on GitHub, and the upgrade to it started.

The releases pass asks GitHub every install.release_check_hours for the factory's newest release, with the owner's
read-only factory token, in the repo root's record of the install names. A release ahead of the installed commit is
offered with its changelog: the subjects of the commits it adds. The owner's Upgrade (coded) pauses the factory,
waits for every agent to settle, and starts root's upgrade unit, which checks the release again itself. The warden
stops while the install runs: what was asked for stays here for the page, and root's progress record says how it went.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from runtime.shared import releases, store, upgrades
from runtime.shared.models import RESTING
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused, ok
from runtime.warden import lifecycle, ops, registry
from runtime.warden.context import Context

log = logging.getLogger(__name__)
KIND = "upgrade"


def offer_path(places: Places) -> Path:
    return places.state / "upgrade-offer.json"


def asked_path(places: Places) -> Path:
    return places.state / "upgrade-asked.json"


def offer(places: Places) -> dict[str, Any]:
    """The newest release ahead of the installed factory, or None; when GitHub was last asked; and why the last
    asking failed, or None."""
    raw = store.read_json_or(offer_path(places), {"available": None, "checked_at": None, "note": None})
    if not isinstance(raw, dict) or set(raw) != {"available", "checked_at", "note"}:
        raise store.StoreError(f"{offer_path(places)}: needs available, checked_at and note")
    return raw


def asked(places: Places) -> dict[str, Any] | None:
    """The upgrade the owner asked for last: from and to, its changelog, when, and the operation that started it."""
    raw = store.read_json_or(asked_path(places), None)
    if raw is not None and (not isinstance(raw, dict) or set(raw) != {"from", "to", "changelog", "at", "op"}):
        raise store.StoreError(f"{asked_path(places)}: needs from, to, changelog, at and op")
    return raw


def poll(ctx: Context) -> None:
    """The releases pass: once each install.release_check_hours, whether a newer factory is ready."""
    settings, places, now = ctx.settings(), ctx.places, ctx.clock.now()
    last, every = offer(places), settings.get_int("install.release_check_hours") * 3600
    if last["checked_at"] is not None and now - last["checked_at"] < every:
        return
    available, note = last["available"], None
    try:
        installed = upgrades.read_installed(places)
        if installed is None:
            available = None
            note = "Upgrade once by hand from a release file; the dashboard offers every upgrade after that."
        else:
            timeout = float(settings.get_int("watch.http_timeout_seconds"))
            rel = releases.Releases.of(places, installed["repo"], timeout, releases.http_get)
            available = _ahead(rel, installed["commit"])
    # telemetry: surfaced — the page shows the note; logged when it starts or changes, not at every pass
    except (releases.ReleaseError, store.StoreError) as exc:
        note = f"GitHub could not be asked for a newer factory: {exc}"
        if note != last["note"]:
            log.warning("warden: the release check failed (%s)", type(exc).__name__)
    store.write_json(offer_path(places), {"available": available, "checked_at": now, "note": note}, mode=0o600)


def _ahead(rel: releases.Releases, installed: str) -> dict[str, Any] | None:
    latest = rel.latest()
    if latest.commit == installed:
        return None
    status, changelog = rel.changes(installed, latest.commit)
    if status != "ahead":
        return None
    return {"commit": latest.commit, "tag": latest.tag, "published": latest.published, "changelog": changelog}


def start(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """The owner's Upgrade to the release the dashboard offered: pause, let every agent settle, start root's unit."""
    places = ctx.places
    commit = args.get("commit")
    available = offer(places)["available"]
    if not isinstance(commit, str) or available is None or available["commit"] != commit:
        raise Refused("stale", "This changed: that release is not the one on offer now")
    with ctx.lock:
        progress, before = upgrades.read_progress(places), asked(places)
        if progress is not None and progress["state"] == upgrades.RUNNING:
            raise Refused("upgrading", "An upgrade is already under way")
        getting_ready = _op(ctx, before["op"]) if before else None
        if getting_ready is not None and getting_ready["state"] in (ops.WAITING, ops.RUNNING):
            raise Refused("upgrading", "An upgrade is already getting ready")
        installed = upgrades.read_installed(places)
        if installed is None:
            raise Refused("upgrade", "Upgrade once by hand from a release file first")
        steps = [("Pausing the factory", lambda: _pause(ctx)),
                 ("Waiting for every agent to settle", lambda: _settled(ctx)),
                 ("Starting the upgrade", lambda: _start(ctx, commit))]  # fmt: skip
        op_id = ops.launch(ctx, KIND, steps, {"to": commit})
        record = {"from": installed["commit"], "to": commit, "changelog": available["changelog"], "at": ctx.clock.iso(),
                  "op": op_id}  # fmt: skip
        store.write_json(asked_path(places), record, mode=0o600)
    return ok(op=op_id)


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
    lifecycle.owner_pause(ctx, {}, None)
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
            raise ops.StepError(f"{', '.join(busy)} did not settle within {wait} minutes; resume the factory, then "
                                "upgrade again")  # fmt: skip
        time.sleep(settings.get_int("watch.tick_seconds"))


def _start(ctx: Context, commit: str) -> str:
    ctx.launcher.start_upgrade(commit)
    return "started; the dashboard restarts while the factory is installed"


def view(ctx: Context) -> dict[str, Any]:
    """Everything the page draws the upgrade from."""
    places = ctx.places
    installed = upgrades.read_installed(places)
    before = asked(places)
    return {
        "installed": installed["commit"] if installed else None,
        "offer": offer(places),
        "asked": before,
        "op": _op(ctx, before["op"]) if before else None,
        "progress": upgrades.read_progress(places),
        "paused": lifecycle.paused_path(ctx).exists(),
    }
