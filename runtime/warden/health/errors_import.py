"""Prod's, staging's and slot 0's GlitchTip issues, filed as requests in the agents inbox: each issue once, and again
when it comes back after its fix reached that environment.

The loop calls poll on every tick; every errors.poll_minutes it reads this machine's shared GlitchTip for each
environment with its read-only token, and keeps the environment's unresolved issues: its projects' slugs start with
its name, so the factory's own project (error_reports.PROJECT) is no environment's. Its first read takes the issues
open then as known, each filed once GlitchTip sees it again. Another issue not filed before is filed from glitchtip.
One filed before is filed again when GlitchTip saw it after the task its request became reached that environment (on
staging for staging, promoted for prod, checked for slot 0). Its severity comes from the issue: a fatal one, or one
with at least errors.severe_events events, is severe and goes to intake at once. Each environment keeps its own
record, errors-<env>.json: the issues it knows with their request, since when it reads, and the timer
runtime/warden/imports.py keeps.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from runtime.queue import formats, intake
from runtime.queue.book import Queue
from runtime.shared import store
from runtime.shared.models import ON_STAGING
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused
from runtime.warden import imports
from runtime.warden.context import Context
from runtime.warden.health import telemetry
from runtime.warden.health.telemetry import Issue

log = logging.getLogger(__name__)

SENDER = "glitchtip"
SLOT_ZERO = f"{telemetry.SLOT}0"
ENVS = (telemetry.PROD, telemetry.STAGING, SLOT_ZERO)
# The status a task reaches when its fix runs in the environment: an issue seen after that came back.
REACHED = {telemetry.PROD: "promoted", telemetry.STAGING: ON_STAGING, SLOT_ZERO: "checked"}
_FRESH: dict[str, Any] = {"filed": {}, "since": None, **imports.fresh()}


def _timed(env: str) -> imports.Timed:
    # A TelemetryError's text names the service, the path and the cause's class, and never a key.
    expected = (telemetry.TelemetryError, Refused, OSError)
    return imports.Timed(f"{env} errors import", f"importing {env}'s errors", "errors.poll_minutes",
                         expected=expected, told=(telemetry.TelemetryError,))  # fmt: skip


def path(places: Places, env: str) -> Path:
    return places.state / f"errors-{env}.json"


def _read(places: Places, env: str) -> dict[str, Any]:
    raw = store.read_json_or(path(places, env), None)
    if raw is None:
        return {**_FRESH, "filed": {}}
    if not isinstance(raw, dict) or raw.keys() != _FRESH.keys() or not isinstance(raw["filed"], dict):
        raise store.StoreError(f"{path(places, env)}: want exactly {sorted(_FRESH)}, filed an object")
    return raw


def note(places: Places) -> str | None:
    """What the last polls found, for `factory status`; None before the first."""
    found = [n for env in ENVS if (n := imports.note(_read(places, env)))]
    return "; ".join(found) if found else None


def poll(ctx: Context) -> None:
    for env in ENVS:
        state = _read(ctx.places, env)

        def work(env: str = env, state: dict[str, Any] = state) -> str:
            try:
                service = telemetry.glitchtip(ctx.places)
            # telemetry: surfaced — no GlitchTip until its set-up runs: `work` returns which as its note
            except telemetry.NotSetUp as exc:
                return str(exc)
            settings = ctx.settings()
            issues = telemetry.unresolved(service, f"{env}-", float(settings.get_int("telemetry.timeout_seconds")),
                                          settings.get_int("errors.max_pages"))  # fmt: skip
            if state["since"] is None:
                return _take_known(ctx, env, state, issues)
            filed = _file_new(ctx, env, state, issues)
            return f"{filed} {'issue' if filed == 1 else 'issues'} filed"

        if imports.run(ctx, _timed(env), state, work):
            store.write_json(path(ctx.places, env), state)


def severity(issue: Issue, severe_events: int) -> str:
    """Severe for a fatal issue or one seen at least ``severe_events`` times; normal otherwise, and when unknown."""
    count = int(issue.count) if issue.count and issue.count.isdigit() else 0
    return "severe" if issue.level == "fatal" or count >= severe_events else "normal"


def _time(text: str | None) -> float | None:
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() if text else None
    # telemetry: expected — a time GlitchTip sent that is not ISO reads as unknown, and nothing is filed again for it
    except ValueError:
        return None


def _take_known(ctx: Context, env: str, state: dict[str, Any], issues: list[Issue]) -> str:
    state["since"] = ctx.clock.iso()
    for issue in issues:
        state["filed"].setdefault(issue.id, {"seen": issue.last_seen})
    store.write_json(path(ctx.places, env), state)
    return f"{len(issues)} open {'issue' if len(issues) == 1 else 'issues'} taken as known"


def _again(queue: Queue, env: str, record: dict[str, str], issue: Issue) -> bool:
    if "seen" not in record:
        return _came_back(queue, env, record, issue)
    seen, known = _time(issue.last_seen), _time(record["seen"])
    return seen is not None and (known is None or seen > known)


def _came_back(queue: Queue, env: str, record: dict[str, str], issue: Issue) -> bool:
    """Whether GlitchTip saw the issue after the task its request became reached ``env``."""
    task = formats.outcome(queue.request(record["request"]).outcome).task
    if not task:
        return False
    reached = [e["at"] for e in queue.read_log(task) if e.get("to") == REACHED[env]]
    seen = _time(issue.last_seen)
    return bool(reached) and seen is not None and seen > formats.epoch(reached[-1])


def _file_new(ctx: Context, env: str, state: dict[str, Any], issues: list[Issue]) -> int:
    """File each issue not filed before, or come back since; oldest first. Each is kept as soon as it is written."""
    queue = Queue.of(ctx)
    severe_events = ctx.settings().get_int("errors.severe_events")
    filed = 0
    for issue in reversed(issues):
        record = state["filed"].get(issue.id)
        if record is not None and not _again(queue, env, record, issue):
            continue
        where, body = request_text(issue, env if record is not None and "request" in record else None)
        with ctx.lock, queue.lock():
            req = intake.file_request(ctx, queue, "agents", SENDER, severity(issue, severe_events), where, body)
        state["filed"][issue.id] = {"request": req.id, "at": req.at}
        store.write_json(path(ctx.places, env), state)
        filed += 1
    return filed


def request_text(issue: Issue, came_back_to: str | None) -> tuple[str, str]:
    """The request's where and body; ``came_back_to`` names the environment its fix reached, for an issue filed again.
    The title and culprit come from whoever sent the event: they are data."""

    def known(value: str | None) -> str:
        return formats.one_line(value) if value else "unknown"

    where = f"{issue.project}:{formats.one_line(issue.culprit)}" if issue.culprit else issue.project
    lead = f"came back after its fix reached {came_back_to}" if came_back_to else "is new"
    body = "\n".join([
        f"GlitchTip issue {issue.id} in {issue.project} {lead}: {known(issue.title)}",
        "",
        f"- Culprit: {known(issue.culprit)}",
        f"- Level: {known(issue.level)}",
        f"- First seen {known(issue.first_seen)}, last seen {known(issue.last_seen)}",
        f"- Events: {known(issue.count)}",
        f"- Link: {known(issue.permalink)}",
    ])  # fmt: skip
    return where, body
