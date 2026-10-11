"""The warden's tick, and the events that start the orchestrator's loop passes.

wardend calls run_tick every watch.tick_seconds. It reads the vitals; probes staging and prod every
watch.probe_seconds (prod every tick while a push to prod is in flight or just landed) and logs each new build they
answer with; moves the outage along; and
posts to the orchestrator's inbox: loop due once watch.loop_minutes pass after its last pass, staging red the moment
staging fails, and triage due when intake.batch_size requests wait or the oldest has waited
intake.batch_max_wait_hours. Last, it files prod's new GlitchTip issues (errors_import) and the users' new reports
(reports) as requests, each on its own timer. A step that fails never stops the steps after it. The orchestrator's
`factory tick` acknowledges each pass.
"""

from __future__ import annotations

import copy
import hashlib
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from runtime.queue import files, format_check, formats, goal, reports, tasks, turn
from runtime.queue.book import Queue
from runtime.shared import models, store
from runtime.shared.models import Probe
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused, ok
from runtime.warden import deploys, errors_import, github_push, heartbeat, inbox, outage, probes, reflections, vitals
from runtime.warden.context import Context

log = logging.getLogger(__name__)

# deployed: the build each environment last answered with, so tasks advance once per new build.
# goal: the hash of GOAL.md the scores were last made from.
_FRESH: dict[str, Any] = {
    "probed": {}, "deployed": {}, "staging_red": False, "loop_due": None, "triage_due": None, "goal": None,
}  # fmt: skip


def _memo_path(places: Places) -> Path:
    return places.state / "loop.json"


def _tick_path(places: Places) -> Path:
    return places.state / "loop-tick.json"


def _memo(places: Places) -> dict[str, Any]:
    raw = store.read_json_or(_memo_path(places), None)
    if raw is None:
        return copy.deepcopy(_FRESH)
    if not isinstance(raw, dict) or raw.keys() != _FRESH.keys():
        raise store.StoreError(f"{_memo_path(places)}: want exactly {sorted(_FRESH)}")
    return raw


def last_tick(places: Places) -> float | None:
    """When the orchestrator last acknowledged a loop pass, for its silence watch; None before its first."""
    raw = store.read_json_or(_tick_path(places), None)
    if raw is None:
        return None
    at = raw.get("at") if isinstance(raw, dict) else None
    if not isinstance(at, int | float):
        raise store.StoreError(f"{_tick_path(places)}: no at")
    return float(at)


def tick(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """The orchestrator finished a loop pass, having acted on what it was handed; the next loop due counts from now."""
    now = ctx.clock.now()
    inbox.acknowledge(ctx.places, ctx.clock)
    store.write_json(_tick_path(ctx.places), {"at": now, "iso": ctx.clock.iso()})
    heartbeat.ping(ctx, heartbeat.ORCHESTRATOR)
    due = formats.iso(now + ctx.settings().get_int("watch.loop_minutes") * 60)
    return ok(message=f"Loop pass recorded; the next loop due comes by {due} unless an event comes first")


def run_tick(
    ctx: Context, fetch: probes.Fetch = probes.http_get, proc: Path = vitals.PROC, supplies: Path = vitals.POWER
) -> None:
    """Each step runs on its own: one that fails is logged with its class, and the steps after it still run."""
    now = ctx.clock.now()
    steps: list[tuple[str, Callable[[], object]]] = [
        ("vitals", lambda: vitals.refresh(ctx, proc, supplies)),
        ("probes", lambda: _probes(ctx, fetch, now)),
        ("loop due", lambda: _loop_due(ctx, _memo(ctx.places), now)),
        ("outage fix", lambda: _fix(ctx)),
        ("merge turn", lambda: turn.find_merge(ctx)),
        ("triage due", lambda: _triage_due(ctx, _memo(ctx.places), now)),
        ("queue format", lambda: format_check.sweep(ctx)),
        ("goal", lambda: _goal(ctx, _memo(ctx.places))),
        ("errors import", lambda: errors_import.poll(ctx)),
        ("reports import", lambda: reports.poll(ctx)),
    ]
    for name, step in steps:
        try:
            step()
        # telemetry: last-resort — the step's failure is logged with its trace, and the tick goes on
        except Exception as exc:
            log.exception("warden: the tick's %s step failed (%s)", name, type(exc).__name__)


def _probes(ctx: Context, fetch: probes.Fetch, now: float) -> None:
    memo = _memo(ctx.places)
    every = ctx.settings().get_int("watch.probe_seconds")
    for env in probes.ENVS:
        last = memo["probed"].get(env)
        close = env == probes.PROD and github_push.watching(ctx)
        if last is None or close or now - last >= every:
            memo["probed"][env] = now
            probe = probes.take(ctx, env, fetch)
            if env == probes.PROD:
                outage.observe(ctx, probe)
            else:
                _staging(ctx, memo, probe)
            _deployed(ctx, memo, probe)
            store.write_json(_memo_path(ctx.places), memo)
            if env == probes.STAGING:
                heartbeat.ping(ctx, heartbeat.WARDEN)


def _staging(ctx: Context, memo: dict[str, Any], probe: Probe) -> None:
    """Staging red is posted once per red spell, at its first counted failure; a network failure changes nothing."""
    if probe.ok:
        memo["staging_red"] = False
    elif probe.counts and not memo["staging_red"]:
        memo["staging_red"] = True
        reflections.incident(ctx, f"Staging went red at {probe.at}; the orchestrator was told to revert or fix it.")
        ctx.post_event(inbox.STAGING_RED, {
            "probe": models.dump(probe),
            "request": {
                "inbox": "agents",
                "severity": "severe",
                "where": "staging",
                "body": f"Staging's health check failed at {probe.at}: {probe.error}. Revert the change that broke "
                        "it at once, or fix it forward.",
            },
        })  # fmt: skip


def _deployed(ctx: Context, memo: dict[str, Any], probe: Probe) -> None:
    """A new build on staging or prod goes in the deploy log, and moves the tasks it includes along: checked to
    on-staging to promoted."""
    if not probe.ok or memo["deployed"].get(probe.env) == probe.commit:
        return
    memo["deployed"][probe.env] = probe.commit
    deploys.record(ctx.places, probe.env, probe.commit, probe.at)
    try:
        moved = tasks.advance_deployed(ctx, memo["deployed"].get(probes.STAGING), memo["deployed"].get(probes.PROD))
    except Refused as exc:
        # Retried on the next probe: forgetting the build makes it new again.
        del memo["deployed"][probe.env]
        log.warning("warden: advancing tasks to the %s build failed (%s)", probe.env, exc.rule)
        return
    if moved:
        log.info("warden: tasks advanced to the %s build: %s", probe.env, ", ".join(moved))


def _goal(ctx: Context, memo: dict[str, Any]) -> None:
    """A change to GOAL.md scores every waiting task again and tells the orchestrator; the first reading is none."""
    settings = ctx.settings()
    raw = files.read_repo_file(Path(settings.get_str("repo.path")), goal.GOAL_FILE,
                               settings.get_int("queue.max_upload_kib") * 1024)  # fmt: skip
    digest = hashlib.sha256(raw).hexdigest()
    if memo["goal"] == digest:
        return
    changed = tasks.rescore(ctx)
    first, memo["goal"] = memo["goal"] is None, digest
    store.write_json(_memo_path(ctx.places), memo)
    if not first:
        ctx.post_event(inbox.GOAL_CHANGED, {"rescored": changed})
    log.info("warden: GOAL.md changed; %d task scores changed", changed)


def _fix(ctx: Context) -> None:
    current = outage.read(ctx.places)
    if current is None or current.stage != outage.FIXING:
        return
    outage.fix_status(ctx, Queue.of(ctx).task(current.task).status)


def _loop_due(ctx: Context, memo: dict[str, Any], now: float) -> None:
    passes = [t for t in (memo["loop_due"], last_tick(ctx.places)) if t is not None]
    if passes and now - max(passes) < ctx.settings().get_int("watch.loop_minutes") * 60:
        return
    if inbox.pending(ctx.places, inbox.LOOP_DUE):
        return
    ctx.post_event(inbox.LOOP_DUE, {})
    memo["loop_due"] = now
    store.write_json(_memo_path(ctx.places), memo)


def _triage_due(ctx: Context, memo: dict[str, Any], now: float) -> None:
    settings = ctx.settings()
    waiting = _waiting(ctx)
    if not waiting:
        return
    ages = [now - at for at in waiting if at is not None]
    oldest = max(ages, default=None)
    due = len(waiting) >= settings.get_int("intake.batch_size") or (
        oldest is not None and oldest >= settings.get_int("intake.batch_max_wait_hours") * 3600
    )
    # A triage pass takes a while and the batch still waits while it runs: one reminder per loop period is enough.
    recent = memo["triage_due"] is not None and now - memo["triage_due"] < settings.get_int("watch.loop_minutes") * 60
    if not due or recent or inbox.pending(ctx.places, inbox.TRIAGE_DUE):
        return
    ctx.post_event(
        inbox.TRIAGE_DUE, {"waiting": len(waiting), "oldest_hours": None if oldest is None else oldest / 3600}
    )
    memo["triage_due"] = now
    store.write_json(_memo_path(ctx.places), memo)


def _waiting(ctx: Context) -> list[float | None]:
    """When each request that waits for triage arrived: no outcome yet, and not severe (those take the fast lane).

    A request that cannot be read still waits, with no known age; the queue-format sweep reports it.
    """
    good, broken = Queue.of(ctx).requests()
    ages: list[float | None] = [formats.epoch(r.at) for r in good if not r.outcome and r.severity != "severe"]
    return ages + [None] * len(broken)
