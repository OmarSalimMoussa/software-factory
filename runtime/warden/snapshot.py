"""`snapshot`: a copy of the raw state the dashboard draws, read under the warden's lock; and the pictures it shows.

The dashboard names what it wants; each name is read on its own, and one that cannot be read comes back as an error
for that name alone, so a broken file fails one part of a page, never the page. Cost, probe streaks and refusals are
folded from their logs as the logs grow (folds.py), so a poll reads only the lines added since the last one.
"""

from __future__ import annotations

import base64
import copy
import dataclasses
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from runtime.guard.rules import RULES
from runtime.qa import evidence as qa_evidence
from runtime.qa.replay import reader as replay_reader
from runtime.queue import approvals, cards, files, goal, intake, pictures, proposals, records, triage
from runtime.queue.book import Queue
from runtime.queue.formats import TASK_ID
from runtime.shared import models, protocol, store
from runtime.shared.models import RETIRED
from runtime.shared.protocol import Refused
from runtime.slots import leases
from runtime.warden import gc, public
from runtime.warden.context import Context
from runtime.warden.delivery import ci_watch, deploys, github_push, outage, prod_push
from runtime.warden.folds import Cost, History, Refusals, Streaks, kept
from runtime.warden.health import probes
from runtime.warden.owner import accounts, codes, factory_upgrade, messages, reflections, settings_save
from runtime.warden.sessions import doing, lifecycle, permits, registry
from runtime.warden.spend import plan

log = logging.getLogger("factory.warden")

Reader = Callable[[Context, dict[str, Any]], Any]
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_IMAGES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
    "svg": "image/svg+xml",
}
# An answer is one JSON line of at most protocol.MAX_LINE bytes, and base64 grows bytes by a third.
_MAX_IMAGE = protocol.MAX_LINE * 3 // 4 - 4096
_MAX_IDS = 200  # task ids one snapshot reads logs, specs or QA records for


# ---- the readers, one per name the dashboard may want ---------------------------------------------------------------


def _ids(ctx: Context, args: dict[str, Any], key: str) -> list[str]:
    """The tasks named in ``key``, and every task whose status is one of ``statuses``."""
    raw, states = args.get(key, []), args.get("statuses", [])
    ids_ok = isinstance(raw, list) and all(isinstance(t, str) and TASK_ID.match(t) for t in raw)
    if not ids_ok or len(raw) > _MAX_IDS or not isinstance(states, list):
        raise Refused("snapshot", f"{key} is a list of at most {_MAX_IDS} task ids, statuses a list of statuses")
    chosen = [t.id for t in Queue.of(ctx).tasks()[0] if t.status in states][:_MAX_IDS] if states else []
    return list(dict.fromkeys([*raw, *chosen]))


def _settings(ctx: Context, args: dict[str, Any]) -> Any:
    current = ctx.settings()
    audit = store.read_jsonl(ctx.places.audit)[-current.get_int("dashboard.page_size") :]
    return {
        "tree": current.tree(),
        "defaults": current.defaults,
        "overrides": current.overrides,
        "version": settings_save.overrides_version(current.overrides),
        "audit": audit,
    }


def _sessions(ctx: Context, args: dict[str, Any]) -> Any:
    current = ctx.settings()
    held = {lease.holder: lease.slot for n in leases.numbers(ctx.places, current)
            if (lease := leases.read(ctx.places, n)) is not None}  # fmt: skip
    found = {}
    for sid, s in registry.read(ctx.places).items():
        row = models.dump(s)
        row["slot"] = held.get(s.name) if s.state != RETIRED and not s.parent else None
        try:
            row["facts"] = doing.facts(ctx.places, sid)
            row["sentence"] = doing.sentence(current, s, row["facts"], row["slot"]) if s.state != RETIRED else s.doing
        # telemetry: surfaced — the agent's `row` draws its sentence as unknown; the rest of the tree still draws
        except store.StoreError:
            row["facts"], row["sentence"] = {}, None
        found[sid] = row
    return found


def _queue(ctx: Context, args: dict[str, Any]) -> Any:
    queue = Queue.of(ctx)
    requests, bad_requests = queue.requests()
    tasks, bad_tasks = queue.tasks()
    checks, bad_checks = queue.checks()
    broken = [{"file": b.file, "error": b.error} for b in (*bad_requests, *bad_tasks, *bad_checks)]
    specs = {}
    for name in queue.files.names("specs"):
        if name.endswith(".md"):
            try:
                spec = records.parse_spec(queue.files.read(f"specs/{name}").decode(), f"specs/{name}")
            # telemetry: surfaced — a broken spec is a row in `broken` that says so; the queue-format sweep reports it
            except (ValueError, UnicodeDecodeError) as exc:
                broken.append({"file": f"specs/{name}", "error": str(exc)})
                continue
            specs[spec.task] = {k: v for k, v in models.dump(spec).items() if k != "body"}
    rows = []
    for task in tasks:
        row = models.dump(task)
        # What only a task's log holds: how it came to be closed, parked or proposed, and whether the owner then
        # changed a proposal's run.
        if task.status in ("closed", "parked", "proposed"):
            log = queue.read_log(task.id)
            last = next((e for e in reversed(log) if e.get("to") == task.status), {})
            row["last_move"] = {k: last.get(k, "") for k in ("from", "event", "at", "text")}
            row["run_changed"] = any(e["event"] == "owner-run" and e["at"] >= last.get("at", "") for e in log)
        rows.append(row)
    return {
        "requests": [models.dump(r) for r in requests],
        "tasks": rows,
        "checks": [models.dump(c) for c in checks],
        "specs": specs,
        "go": [t.id for t in tasks if approvals.has_go(ctx.places, t.id)],
        "boosts": intake.boosts(ctx),
        "broken": broken,
    }


def _logs(ctx: Context, args: dict[str, Any]) -> Any:
    queue = Queue.of(ctx)
    return {tid: queue.read_log(tid) for tid in _ids(ctx, args, "tasks")}


def _spec_text(ctx: Context, args: dict[str, Any]) -> Any:
    queue = Queue.of(ctx)
    return {tid: (lambda s: None if s is None else models.dump(s))(queue.spec(tid)) for tid in _ids(ctx, args, "specs")}


def _qa(ctx: Context, args: dict[str, Any]) -> Any:
    wanted = set(_ids(ctx, args, "tasks"))
    sessions = [s for s in registry.read(ctx.places).values() if s.task in wanted and not s.parent and s.worktree]
    found: dict[str, Any] = {tid: {"ready": [], "results": [], "evidence": []} for tid in wanted}
    for s in sorted(sessions, key=lambda s: s.started):
        ready = store.read_json_or(ctx.places.ready(s.id), {"records": []})
        found[s.task]["ready"] += ready.get("records", []) if isinstance(ready, dict) else []
        found[s.task]["results"] += store.read_jsonl(ctx.places.state / "qa-results" / f"{s.id}.jsonl")
    for entry in store.read_jsonl(ctx.places.evidence / "index.jsonl"):
        if entry.get("task") in wanted:
            found[entry["task"]]["evidence"].append(entry)
    return found


def _probes(ctx: Context, args: dict[str, Any]) -> Any:
    strip = ctx.settings().get_int("dashboard.probe_strip")
    streaks = kept(Streaks, ctx.places.probes)
    streaks.read(ctx.places.probes)
    return {
        "recent": {env: [models.dump(p) for p in probes.recent(ctx.places, env, strip)] for env in probes.ENVS},
        "streak": dict(streaks.state),
    }


def _cost(ctx: Context, args: dict[str, Any]) -> Any:
    days = args.get("days", 1)
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 366:
        raise Refused("snapshot", "days is a whole number from 1 to 366")
    book = kept(Cost, ctx.places.cost)
    book.read(ctx.places.cost)
    # A copy: the answer is sent after the lock is let go, while the next snapshot may fold new calls in.
    return copy.deepcopy(book.answer(days))


def _thread(ctx: Context, args: dict[str, Any]) -> Any:
    """The owner's messages to one session and its replies, read back from its transcript: only for an open panel."""
    ref = args.get("session")
    if not isinstance(ref, str) or not ref:
        raise Refused("snapshot", "session names the agent whose thread to read")
    return messages.thread(ctx, lifecycle.find(ctx, lifecycle.read(ctx), ref))


def _history(ctx: Context, args: dict[str, Any]) -> Any:
    book = kept(History, ctx.places.history)
    book.read(ctx.places.history)
    return book.answer()


def _refusals(ctx: Context, args: dict[str, Any]) -> Any:
    tally = kept(Refusals, ctx.places.refusals)
    tally.read(ctx.places.refusals)
    return dict(tally.days.get(ctx.clock.iso()[:10], {}))


def _slots(ctx: Context, args: dict[str, Any]) -> Any:
    current = ctx.settings()
    rows = []
    for n in leases.numbers(ctx.places, current):
        lease = leases.read(ctx.places, n)
        held = None if lease is None else {"holder": lease.holder, "since": lease.since, "note": lease.note}
        rows.append({"n": n, "port": leases.port(current, n), "lease": held})
    return {"rows": rows, "main_port": leases.port(current, 0)}


def _goal(ctx: Context, args: dict[str, Any]) -> Any:
    current = ctx.settings()
    repo = Path(current.get_str("repo.path"))
    limit = current.get_int("queue.max_upload_kib") * 1024
    if not (repo / goal.GOAL_FILE).is_file():
        raise Refused("goal", f"{goal.GOAL_FILE} is missing, so the goal cannot be drawn.")
    try:
        items = goal.read(repo, limit)
        raw = files.read_repo_file(repo, goal.GOAL_FILE, limit)
    except (goal.GoalError, OSError, UnicodeDecodeError) as exc:
        raise Refused("goal", f"{goal.GOAL_FILE} cannot be read: {exc}") from exc
    return {
        "items": [{"id": i.id, "text": i.text, "done": i.done, "rank": i.rank} for i in items],
        "text": raw.decode(),
        "changed": (repo / goal.GOAL_FILE).stat().st_mtime,
    }


def _memory() -> int | None:
    """The laptop's memory in kB, so free memory can be drawn as a share of it."""
    try:
        found = re.search(r"^MemTotal:\s+(\d+) kB", Path("/proc/meminfo").read_text(), re.MULTILINE)
    # telemetry: surfaced — the memory bar draws as unknown from what `_memory` returns
    except OSError:
        return None
    return int(found.group(1)) if found else None


def _factory(ctx: Context, args: dict[str, Any]) -> Any:
    paused = store.read_json_or(lifecycle.paused_path(ctx), None)
    until = codes.locked_until(ctx.places, ctx.clock.now())
    triage = store.read_json_or(ctx.places.state / "triage-last.json", None)
    return {"paused": paused, "locked_until": until, "triage": triage}


def _staging_pinned(ctx: Context) -> str | None:
    """Why staging's build may not go to prod, as the push would refuse it; None when it may."""
    on_default = deploys.on_default(ctx.places, probes.STAGING)
    return None if on_default is True else prod_push.staging_off_default(ctx.settings(), on_default)


def _proposals(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    found = proposals.read(ctx.places)
    # An empty place is the orchestrator choosing only while it is at work.
    return {"open": found.open, "approved": found.approved,
            "choosing": permits.at_work(registry.read(ctx.places).values())}  # fmt: skip


READERS: dict[str, Reader] = {
    "settings": _settings,
    "sessions": _sessions,
    "proposals": _proposals,
    "cards": lambda ctx, args: [models.dump(c) for c in cards.read(ctx.places)],
    "outage": lambda ctx, args: (lambda o: None if o is None else models.dump(o))(outage.read(ctx.places)),
    "factory": _factory,
    "probes": _probes,
    "deploys": lambda ctx, args: {
        **{env: deploys.builds(ctx.places, env) for env in probes.ENVS},
        "push_back": prod_push.push_back_target(ctx),
        # Why staging's build may not go to prod, as the push would refuse it; None when it may.
        "staging_pinned": _staging_pinned(ctx),
    },
    "ci": lambda ctx, args: ci_watch.read(ctx.places),
    "staging_push": lambda ctx, args: github_push.waiting(ctx.places),
    "upgrade": lambda ctx, args: factory_upgrade.view(ctx),
    "plan": lambda ctx, args: plan.reading(ctx),
    "vitals": lambda ctx, args: {"reading": store.read_json_or(ctx.places.vitals, None), "memory": _memory()},
    "rules": lambda ctx, args: {"rules": RULES, "off": sorted(public.read_switches(ctx.places))},
    "refusals": _refusals,
    "slots": _slots,
    "queue": _queue,
    "logs": _logs,
    "specs": _spec_text,
    "qa": _qa,
    "replay": replay_reader.read,
    "goal": _goal,
    "triage": lambda ctx, args: dataclasses.asdict(triage.batch(ctx)),
    "docker": lambda ctx, args: store.read_json_or(gc.disk_path(ctx.places), None),
    "reflections": lambda ctx, args: reflections.entries(ctx.places),
    "history": _history,
    "thread": _thread,
    "cost": _cost,
    "accounts": lambda ctx, args: accounts.listing(ctx),
}


def _read_one(ctx: Context, name: str, args: dict[str, Any]) -> tuple[Any, str | None]:
    try:
        return READERS[name](ctx, args), None
    # telemetry: surfaced — the part that needs it draws the `reason` in its place
    except Refused as exc:
        return None, exc.reason
    except (store.StoreError, models.RecordError, ValueError, OSError) as exc:
        log.warning("warden: the snapshot could not read %s (%s)", name, type(exc).__name__)
        return None, f"The warden could not read it: {exc}"


def snapshot(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """{"want": [names], "tasks"?: [ids], "specs"?: [ids], "days"?: n, "replay"?: {run}} → each name's state, or its
    error."""
    want = args.get("want")
    if not isinstance(want, list) or not want or not all(isinstance(w, str) for w in want):
        raise Refused("snapshot", f"want is a list of: {', '.join(sorted(READERS))}")
    unknown = sorted(set(want) - set(READERS))
    if unknown:
        raise Refused("snapshot", f"no such state: {', '.join(unknown)}")
    state: dict[str, Any] = {}
    errors: dict[str, str] = {}
    with ctx.lock:
        now = ctx.clock.now()
        for name in dict.fromkeys(want):
            state[name], error = _read_one(ctx, name, args)
            if error is not None:
                errors[name] = error
                del state[name]
    return {"now": now, "state": state, "errors": errors}


# ---- pictures and QA evidence, as bytes for the dashboard to serve -------------------------------------------------


def picture(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """{"kind": "task"|"card", "id"} → the picture's type and bytes."""
    kind, ref = args.get("kind"), args.get("id")
    if not isinstance(ref, str) or not _SEGMENT.match(ref):
        raise Refused("picture", "name a task or a card by its id")
    queue = Queue.of(ctx)
    if kind == "task":
        name = queue.task(ref).picture.removeprefix("pictures/")
    elif kind == "card":
        name = next((c.picture for c in cards.read(ctx.places) if c.id == ref), "")
    else:
        raise Refused("picture", "a picture belongs to a task or a card")
    if not name:
        raise Refused("picture", f"{ref} has no picture")
    try:
        raw, media = pictures.picture_bytes(ctx.places, queue, kind, name)
    except FileNotFoundError as exc:
        raise Refused("picture", f"the picture of {ref} is gone") from exc
    return {"type": media, "bytes_b64": base64.b64encode(raw).decode()}


def evidence(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """{"task", "path"} → one evidence image under the task's folder, opened without following any link."""
    task, rel = args.get("task"), args.get("path")
    if not isinstance(task, str) or not TASK_ID.match(task) or not isinstance(rel, str):
        raise Refused("evidence", "name a task and a file under its evidence")
    parts = rel.split("/")
    media = _IMAGES.get(parts[-1].rpartition(".")[2].lower())
    if not all(_SEGMENT.match(p) for p in parts) or media is None:
        raise Refused("evidence", "evidence is shown only as an image named inside the task's folder")
    try:
        raw = qa_evidence.read_file(ctx.places, [task, *parts], _MAX_IMAGE)
    except FileNotFoundError as exc:
        raise Refused("evidence", f"{task} has no evidence {rel}; it may have aged out") from exc
    except qa_evidence.TooLarge as exc:
        raise Refused("evidence", f"{rel} is too large to show here") from exc
    except OSError as exc:
        raise Refused("evidence", f"{rel} is not a plain file in {task}'s evidence") from exc
    return {"type": media, "bytes_b64": base64.b64encode(raw).decode()}
