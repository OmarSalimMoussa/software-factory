"""The warden's tick, and the events that start the orchestrator's loop passes.

wardend calls run_tick every watch.tick_seconds. It reads the vitals; probes staging and prod every
watch.probe_seconds (prod every tick while a push to prod is in flight or just landed) and logs each new build they
answer with; moves the outage along; and
posts to the orchestrator's inbox: loop due once watch.loop_minutes pass after its last pass, staging red the moment
staging fails, and triage due when intake.batch_size requests wait or the oldest has waited
intake.batch_max_wait_hours. Last, it files prod's, staging's and slot 0's new GlitchTip issues (errors_import) as
requests, on its own timer; the users' reports come in a pass of the warden's own (runtime/queue/reports.py), off the
tick. A step that fails never stops the steps after it. The orchestrator's `factory tick` acknowledges each pass.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from runtime.queue import files, format_check, formats, goal, intake, tasks, triage, turn
from runtime.queue.book import Queue, request_rel
from runtime.shared import models, store
from runtime.shared.models import Probe
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused, ok
from runtime.warden import inbox
from runtime.warden.context import Context
from runtime.warden.delivery import deploys, github_push, outage, prod_push
from runtime.warden.health import container_events, errors_import, heartbeat, probes, vitals
from runtime.warden.owner import reflections

log = logging.getLogger(__name__)

# deployed: the build each environment last answered with, so tasks advance once per new build.
# staging_red: the request the warden filed for staging's red spell, "" while it is not red.
# goal: the hash of GOAL.md the scores were last made from.
_FRESH: dict[str, Any] = {
    "probed": {}, "deployed": {}, "staging_red": "", "loop_due": None, "triage_due": None, "goal": None,
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
        ("rescore", lambda: _rescore(ctx, now)),
        ("errors import", lambda: errors_import.poll(ctx)),
        ("daily qa-prod", lambda: _qa_prod_due(ctx, now)),
        ("container events", lambda: container_events.run_pass(ctx)),
    ]
    for name, step in steps:
        run_once(ctx, f"the tick's {name} step", step)


# Each step's or pass's current failure, by the warden's state folder and its name.
_FAILING: dict[tuple[str, str], str] = {}


def run_once(ctx: Context, what: str, work: Callable[[], object]) -> None:
    """Run ``work``; a failure is logged with its trace when it begins or changes, not again each time it repeats
    (a missing GOAL.md fails every tick), and its end is logged too. The caller goes on either way."""
    key = (str(ctx.places.state), what)
    try:
        work()
    except Exception as exc:
        said = f"{type(exc).__name__}: {exc}"[:300]
        if _FAILING.get(key) != said:
            log.exception("warden: %s failed (%s)", what, type(exc).__name__)
        _FAILING[key] = said
    else:
        if _FAILING.pop(key, None) is not None:
            log.info("warden: %s works again", what)


def _probes(ctx: Context, fetch: probes.Fetch, now: float) -> None:
    memo = _memo(ctx.places)
    every = ctx.settings().get_int("watch.probe_seconds")
    for env in probes.ENVS:
        last = memo["probed"].get(env)
        close = env == probes.PROD and prod_push.watching(ctx)
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
    """Staging red is posted once per red spell, after watch.outage_after counted failures in a row, as for prod;
    a network failure changes nothing, nor does a failure while the deploy of the warden's last push lands. The
    event and the request name that push as the first suspect; reverting stays the orchestrator's call."""
    if probe.ok:
        red, memo["staging_red"] = memo["staging_red"], ""
        if isinstance(red, str) and red:
            _recovered(ctx, red, memo["deployed"].get(probe.env), probe)
        return
    if not probe.counts or memo["staging_red"]:
        return
    if deploys.in_flight(ctx.places, probes.STAGING, ctx.clock.now()):
        return
    needed = ctx.settings().get_int("watch.outage_after")
    streak = probes.recent(ctx.places, probes.STAGING, needed)
    if len(streak) < needed or not all(p.counts for p in streak):
        return
    suspect = deploys.builds(ctx.places, probes.STAGING)["current"]
    pushed = deploys.last_push(ctx.places, probes.STAGING)
    body = (f"Staging's health check failed {needed} probes in a row since {streak[0].at}: {probe.error}. "
            + (f"The warden's last push, {pushed[:12]}, is the first suspect. " if pushed else "")
            + "Revert the change that broke it at once, or fix it forward.")  # fmt: skip
    memo["staging_red"] = req = file_red(ctx, "staging", body)
    reflections.incident(ctx, f"Staging went red at {streak[0].at}; the orchestrator was told to revert or fix it.")
    ctx.post_event(inbox.STAGING_RED, {"probe": models.dump(probe), "request": req, "suspect": pushed or None,
                                       "last_good": suspect["commit"] if suspect else None})  # fmt: skip


def _recovered(ctx: Context, red: str, last_good: str | None, probe: Probe) -> None:
    """The red spell's end, added to the request the warden filed for it: on the build it went red on, or a new one."""
    build = probe.commit[:12]
    if build and probe.commit == last_good:
        how = f", on build {build}, the one it went red on"
    else:
        how = f", on a new build, {build}" if build else ""
    queue = Queue.of(ctx)
    try:
        with ctx.lock, queue.lock():
            req = queue.request(red)
            body = f"{req.body}\n\nStaging answered healthy again at {probe.at}{how}."
            queue.write({request_rel(req): formats.dump_request(dataclasses.replace(req, body=body))})
    except (OSError, formats.FormatError, Refused, store.StoreError) as exc:
        log.warning("warden: noting staging's recovery on %s failed (%s)", red, type(exc).__name__)


def file_red(ctx: Context, where: str, body: str) -> str:
    """The warden's own severe request for a red environment; its id, for the event that says so."""
    queue = Queue.of(ctx)
    with ctx.lock, queue.lock():
        return intake.file_request(ctx, queue, "agents", "warden", "severe", where, body).id


def _deployed(ctx: Context, memo: dict[str, Any], probe: Probe) -> None:
    """A new build on staging or prod goes in the deploy log, and moves the tasks it includes along: checked to
    on staging to promoted."""
    if not probe.ok or memo["deployed"].get(probe.env) == probe.commit:
        return
    memo["deployed"][probe.env] = probe.commit
    deploys.record(ctx.places, probe.env, probe.commit, probe.at)
    includes = github_push.includes(ctx)
    try:
        found = memo["deployed"]
        moved = tasks.advance_deployed(ctx, found.get(probes.STAGING), found.get(probes.PROD), includes)
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


def _qa_prod_due(ctx: Context, now: float) -> None:
    """A read-only walk of prod qa.prod_every_hours after the last one asked for, on the build prod runs now. The
    first tick only starts the count."""
    path = prod_push.qa_prod_path(ctx.places)
    last = store.read_json_or(path, None)
    if last is None:
        store.write_json(path, {"at": now, "iso": ctx.clock.iso()})
        return
    if (
        not isinstance(last, dict)
        or not isinstance(last.get("at"), int | float)
        or not isinstance(last.get("iso"), str)
    ):
        raise store.StoreError(f"{path}: want at and iso")
    if now - last["at"] < ctx.settings().get_int("qa.prod_every_hours") * 3600:
        return
    current = deploys.builds(ctx.places, probes.PROD)["current"]
    if current is None:
        return  # prod has answered on no build yet: nothing to walk
    prod_push.post_qa_prod(ctx, current["commit"], "daily", last["iso"])


def _rescore(ctx: Context, now: float) -> None:
    """Every waiting task scored again at once, every intake.rescore_hours: a score's age part is fixed when it is
    written, and the order reads the scores side by side."""
    at = tasks.rescored_at(ctx.places)
    if at is not None and now - at < ctx.settings().get_int("intake.rescore_hours") * 3600:
        return
    tasks.rescore(ctx)


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
    """`triage due` names its batch. A boosted request goes at once; otherwise a pass takes a while and the batch
    waits while it runs, so one reminder per loop period is enough."""
    found = triage.batch(ctx)
    if not found.due or inbox.pending(ctx.places, inbox.TRIAGE_DUE):
        return
    period = ctx.settings().get_int("watch.loop_minutes") * 60
    recent = memo["triage_due"] is not None and now - memo["triage_due"] < period
    if recent and not found.boosted:
        return
    last = inbox.last(ctx.places, inbox.TRIAGE_DUE)
    if found.boosted and recent and last is not None and last["data"]["batch"] == found.ids:
        return  # the boosted request was posted at once, and its pass is under way
    ctx.post_event(inbox.TRIAGE_DUE, {"batch": found.ids, "boosted": found.boosted, "waiting": found.waiting})
    memo["triage_due"] = now
    store.write_json(_memo_path(ctx.places), memo)
