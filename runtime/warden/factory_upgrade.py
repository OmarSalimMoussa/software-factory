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
from datetime import datetime
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


OFFER_KEYS = {"available", "checked_at", "note", "base"}


def offer(places: Places) -> dict[str, Any]:
    """The newest release ahead of ``base`` (the install it was asked for), when GitHub was last asked, and why that
    failed."""
    raw = store.read_json_or(offer_path(places), {"available": None, "checked_at": None, "note": None, "base": None})
    if not isinstance(raw, dict) or set(raw) != OFFER_KEYS:
        raise store.StoreError(f"{offer_path(places)}: needs {', '.join(sorted(OFFER_KEYS))}")
    return raw


def _for_installed(places: Places) -> dict[str, Any] | None:
    """What is on offer for the factory installed now: none when it was asked for another install."""
    installed = upgrades.read_installed(places)
    found = offer(places)
    return found["available"] if installed is not None and found["base"] == installed["commit"] else None


def asked(places: Places) -> dict[str, Any] | None:
    """The upgrade the owner asked for last: from and to, its changelog, when, and the operation that started it."""
    raw = store.read_json_or(asked_path(places), None)
    if raw is not None and (not isinstance(raw, dict) or set(raw) != {"from", "to", "changelog", "at", "op"}):
        raise store.StoreError(f"{asked_path(places)}: needs from, to, changelog, at and op")
    return raw


def poll(ctx: Context) -> None:
    settings, places, now = ctx.settings(), ctx.places, ctx.clock.now()
    last, every = offer(places), settings.get_int("install.release_check_hours") * 3600
    available, note, base = None, None, None
    try:
        installed = upgrades.read_installed(places)
        base = installed["commit"] if installed else None
        # A new install asks again at once: what was on offer may be the very release it installed.
        if base == last["base"] and last["checked_at"] is not None and now - last["checked_at"] < every:
            return
        if installed is None:
            note = "Upgrade once by hand from a release file; the dashboard offers every upgrade after that."
        else:
            timeout = float(settings.get_int("watch.http_timeout_seconds"))
            rel = releases.Releases.of(releases.token_path(places), installed["repo"], timeout, releases.http_get)
            available = _ahead(rel, installed["commit"])
    # telemetry: surfaced — the page shows the note; logged when it starts or changes, not at every pass
    except (releases.ReleaseError, store.StoreError) as exc:
        available = last["available"] if base == last["base"] else None
        note = f"GitHub could not be asked for a newer factory: {exc}"
        if note != last["note"]:
            log.warning("warden: the release check failed (%s)", type(exc).__name__)
    record = {"available": available, "checked_at": now, "note": note, "base": base}
    store.write_json(offer_path(places), record, mode=0o600)


def _ahead(rel: releases.Releases, installed: str) -> dict[str, Any] | None:
    latest = rel.latest()
    if latest.commit == installed:
        return None
    status, changelog = rel.changes(installed, latest.commit)
    if status != "ahead":
        return None
    return {"commit": latest.commit, "tag": latest.tag, "published": latest.published, "changelog": changelog}


def start(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    places = ctx.places
    commit = args.get("commit")
    available = _for_installed(places)
    if not isinstance(commit, str) or available is None or available["commit"] != commit:
        raise Refused("stale", "This changed: that release is not the one on offer now")
    with ctx.lock:
        progress, before = progress_now(ctx), asked(places)
        if progress is not None and progress["state"] == upgrades.RUNNING:
            raise Refused("upgrading", "An upgrade is already under way")
        getting_ready = _op(ctx, before["op"]) if before else None
        if getting_ready is not None and getting_ready["state"] in (ops.WAITING, ops.RUNNING):
            raise Refused("upgrading", "An upgrade is already getting ready")
        installed = upgrades.read_installed(places)
        if installed is None:
            raise Refused("upgrade", "Upgrade once by hand from a release file first")
        steps = [
            ("Pausing the factory", lambda: _pause(ctx)),
            ("Waiting for every agent to settle", lambda: _settled(ctx)),
            ("Starting the upgrade", lambda: _start(ctx, commit)),
        ]
        op_id = ops.launch(ctx, KIND, steps, {"to": commit})
        record = {"from": installed["commit"], "to": commit, "changelog": available["changelog"], "op": op_id}
        record["at"] = ctx.clock.iso()
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
        "offer": {**offer(places), "available": _for_installed(places)},
        "asked": before,
        "op": _op(ctx, before["op"]) if before else None,
        "progress": progress_now(ctx) if before else None,
        "paused": lifecycle.paused_path(ctx).exists(),
    }
