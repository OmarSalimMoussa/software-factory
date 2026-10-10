"""Prod's new GlitchTip issues, filed as requests in the agents inbox: each issue once, ever.

The loop calls poll on every tick; it reads the server's GlitchTip every errors.poll_minutes, with the read-only token
the server set-up saved, keeps the unresolved issues of prod's projects and files each one it has not seen, from
glitchtip, severity normal. errors-seen.json keeps the ids it filed, when it last read, whether the reads are failing,
and the note `factory status` shows. A read that fails is logged once per run of failures and tried again at the next
poll; it never stops the loop.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from runtime.queue import formats, intake
from runtime.queue.book import Queue
from runtime.shared import store
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused
from runtime.warden import telemetry
from runtime.warden.context import Context
from runtime.warden.telemetry import Issue

log = logging.getLogger(__name__)

SENDER = "glitchtip"
PREFIX = f"{telemetry.PROD}-"
NOT_SET_UP = "errors import: the server set-up has not run"
_FRESH: dict[str, Any] = {"seen": [], "polled": None, "failing": False, "note": None}
# What a failing read is expected to raise; anything else is also caught, so the loop goes on, but logged with its
# trace.
_EXPECTED = (telemetry.TelemetryError, Refused, OSError)


def path(places: Places) -> Path:
    return places.state / "errors-seen.json"


def _read(places: Places) -> dict[str, Any]:
    raw = store.read_json_or(path(places), None)
    if raw is None:
        return {**_FRESH, "seen": []}
    if not isinstance(raw, dict) or raw.keys() != _FRESH.keys() or not isinstance(raw["seen"], list):
        raise store.StoreError(f"{path(places)}: want exactly {sorted(_FRESH)}, seen a list")
    return raw


def note(places: Places) -> str | None:
    """What the last poll found, for `factory status`; None before the first."""
    found = _read(places)["note"]
    return found if isinstance(found, str) else None


def poll(ctx: Context) -> None:
    state = _read(ctx.places)
    now = ctx.clock.now()
    settings = ctx.settings()
    if state["polled"] is not None and now - state["polled"] < settings.get_int("errors.poll_minutes") * 60:
        return
    state["polled"] = now
    try:
        service = telemetry.glitchtip(ctx.places, telemetry.PROD)
        issues = telemetry.unresolved(service, PREFIX, float(settings.get_int("telemetry.timeout_seconds")),
                                      settings.get_int("errors.max_pages"))  # fmt: skip
        filed = _file_new(ctx, state, issues)
    except telemetry.NotSetUp:
        state["failing"], state["note"] = False, NOT_SET_UP
    except Exception as exc:
        # A TelemetryError's text names the service, the path and the cause's class, and never a key.
        why = str(exc) if isinstance(exc, telemetry.TelemetryError) else type(exc).__name__
        if not state["failing"]:
            log.warning("warden: importing prod's errors failed (%s); it runs again at the next poll", why,
                        exc_info=not isinstance(exc, _EXPECTED))  # fmt: skip
        state["failing"] = True
        state["note"] = f"errors import: the read at {ctx.clock.iso()} failed ({why}); it runs again at the next poll"
    else:
        state["failing"] = False
        state["note"] = f"errors import: read at {ctx.clock.iso()}; {filed} new {'issue' if filed == 1 else 'issues'}"
    store.write_json(path(ctx.places), state)


def _file_new(ctx: Context, state: dict[str, Any], issues: list[Issue]) -> int:
    """File each issue not filed before, oldest first; each id is kept as soon as its request is written."""
    queue = Queue.of(ctx)
    filed = 0
    for issue in reversed(issues):
        if issue.id in state["seen"]:
            continue
        where, body = request_text(issue)
        with ctx.lock, queue.lock():
            intake.file_request(ctx, queue, "agents", SENDER, "normal", where, body)
        state["seen"].append(issue.id)
        store.write_json(path(ctx.places), state)
        filed += 1
    return filed


def request_text(issue: Issue) -> tuple[str, str]:
    """The request's where and body. The title and culprit come from whoever sent the event: they are data."""

    def known(value: str | None) -> str:
        return formats.one_line(value) if value else "unknown"

    where = f"{issue.project}:{formats.one_line(issue.culprit)}" if issue.culprit else issue.project
    body = "\n".join([
        f"GlitchTip issue {issue.id} in {issue.project}: {known(issue.title)}",
        "",
        f"- Culprit: {known(issue.culprit)}",
        f"- Level: {known(issue.level)}",
        f"- First seen {known(issue.first_seen)}, last seen {known(issue.last_seen)}",
        f"- Events: {known(issue.count)}",
        f"- Link: {known(issue.permalink)}",
    ])  # fmt: skip
    return where, body
