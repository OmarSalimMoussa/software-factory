"""Plan limits: the subscription's 5-hour and 7-day windows, as the orchestrator's status line last read them.

The status line writes ``plan.json`` in the agent user's cache after each reply, stamped ``written_at`` by its own
clock. The warden copies a well-formed reading into its state when the file changes. A reading is aged by that stamp,
never by the file's times: older than ``cost.plan_stale_minutes`` it is unknown, never the last numbers.

Once a live weekly reading reaches ``cost.plan_pause_percent``, the warden pauses the factory as the owner's pause
does, says why on the pause and in Self-reflect, and leaves the resume to the owner; it never switches accounts. It
pauses once each time the use reaches the setting: kept in plan-pause.json, the setting it held at lets a resume over
it stand until the setting changes or the use falls under it, as in a new week. A weekly reading from before its
window's reset is unknown, as an old one is.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runtime.shared import store
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused
from runtime.warden.context import Context
from runtime.warden.owner import reflections
from runtime.warden.sessions import lifecycle, revive

log = logging.getLogger("factory.warden")

WINDOWS = ("five_hour", "seven_day")
# A reading is under 200 bytes; anything far larger is not one the status line wrote.
_MAX_BYTES = 4096


class PlanError(ValueError):
    """The status line's file is not a reading of the plan's windows."""


def source(agent_home: Path, places: Places) -> Path:
    """Where the status line writes: the agent user's cache folder named after the install folder."""
    return agent_home / ".cache" / places.install.name / "plan.json"


def _number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _window(raw: object) -> dict[str, float] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or not _number(raw.get("used_percentage")) or not _number(raw.get("resets_at")):
        raise PlanError("a window needs used_percentage and resets_at")
    if not 0 <= raw["used_percentage"] <= 100:
        raise PlanError("used_percentage must be from 0 to 100")
    return {"used_percentage": raw["used_percentage"], "resets_at": raw["resets_at"]}


def parse(text: bytes) -> dict[str, Any]:
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise PlanError("not JSON") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("written_at"), str):
        raise PlanError("needs written_at")
    try:
        written = datetime.strptime(raw["written_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise PlanError("written_at is not a UTC time") from exc
    return {"written_at": written.timestamp(), **{name: _window(raw.get(name)) for name in WINDOWS}}


def _read_source(path: Path) -> bytes:
    """The file's bytes, opened without following a link the agent user could plant."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_BYTES:
            raise PlanError("not a small regular file")
        return os.read(fd, _MAX_BYTES + 1)
    finally:
        os.close(fd)


class PlanWatch:
    """Copies each new reading; a broken file is logged once, and the last good reading ages out by its own time."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._seen: tuple[int, int, int] | None = None

    def run_pass(self, ctx: Context) -> None:
        try:
            info = os.lstat(self.path)
        # telemetry: expected — the orchestrator has not replied since its start, so there is no reading yet
        except FileNotFoundError:
            return
        identity = (info.st_ino, info.st_mtime_ns, info.st_size)
        if identity == self._seen:
            return
        self._seen = identity
        try:
            reading = parse(_read_source(self.path))
        except (PlanError, OSError) as exc:
            log.warning("warden: the plan limits file cannot be read (%s)", type(exc).__name__)
            return
        store.write_json(ctx.places.plan, {**reading, "copied_at": ctx.clock.now()}, mode=0o600)


def reading(ctx: Context) -> dict[str, Any]:
    """The plan limits for the dashboard: ``fresh`` with both windows, ``stale`` with none, or ``missing``."""
    raw = store.read_json_or(ctx.places.plan, None)
    if raw is None:
        return {"state": "missing", "age_seconds": None, "five_hour": None, "seven_day": None}
    age = int(ctx.clock.now() - raw["written_at"])
    if age > ctx.settings().get_int("cost.plan_stale_minutes") * 60:
        return {"state": "stale", "age_seconds": age, "five_hour": None, "seven_day": None}
    return {"state": "fresh", "age_seconds": age, **{name: raw[name] for name in WINDOWS}}


def pause_at_limit(ctx: Context) -> None:
    weekly, held = reading(ctx)["seven_day"], ctx.places.state / "plan-pause.json"
    if weekly is None or weekly["resets_at"] <= ctx.clock.now():
        return
    at, used = ctx.settings().get_int("cost.plan_pause_percent"), weekly["used_percentage"]
    if used < at:
        held.unlink(missing_ok=True)
        return
    if store.read_json_or(held, None) == {"percent": at}:
        return
    if not lifecycle.paused_path(ctx).exists():
        why = f"The plan's weekly usage reached {used:g}%, at or past the pause at {at}%."
        try:
            revive.pause(ctx, why)
        # telemetry: expected — a session is coming back, or the owner paused first; the next pass looks again
        except Refused:
            return
        reflections.incident(ctx, f"The warden paused the factory. {why}")
    store.write_json(held, {"percent": at})
