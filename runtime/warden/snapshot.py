"""`snapshot`: a copy of the raw state the dashboard draws, read under the warden's lock; and the pictures it shows.

The dashboard names what it wants; each name is read on its own, and one that cannot be read comes back as an error
for that name alone, so a broken file fails one part of a page, never the page. Cost, probe streaks and refusals are
folded from their logs as the logs grow, so a poll reads only the lines added since the last one. GitHub's
deployments are asked in the background, at most once per watch.probe_seconds, never while the lock is held.
"""

from __future__ import annotations

import base64
import copy
import json
import logging
import os
import re
import stat
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from runtime.guard.rules import RULES
from runtime.queue import approvals, cards, files, goal, intake, pictures, proposals, records
from runtime.queue.book import Queue
from runtime.queue.formats import TASK_ID
from runtime.shared import models, protocol, store
from runtime.shared.models import RETIRED
from runtime.shared.protocol import Refused
from runtime.slots import leases
from runtime.warden import codes, doing, gc, outage, plan, probes, public, reflections, registry, settings_save
from runtime.warden.context import Context
from runtime.warden.github import GitHubError
from runtime.warden.lifecycle import paused_path

log = logging.getLogger("factory.warden")

Reader = Callable[[Context, dict[str, Any]], Any]
ENVIRONMENTS = ("staging", "prod")  # the GitHub environments CI deploys to
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


class _Fold:
    """A JSON-lines log folded as it grows: each new whole line is stepped once; a replaced log starts over."""

    def __init__(self) -> None:
        self.inode, self.offset = -1, 0
        self.reset()

    def reset(self) -> None:
        raise NotImplementedError

    def step(self, raw: dict[str, Any]) -> None:
        raise NotImplementedError

    def read(self, path: Path) -> None:
        try:
            info = path.stat()
        # telemetry: expected — nothing was logged yet
        except FileNotFoundError:
            return
        if info.st_ino != self.inode or info.st_size < self.offset:
            self.inode, self.offset = info.st_ino, 0
            self.reset()
        with path.open("rb") as fh:
            fh.seek(self.offset)
            data = fh.read(info.st_size - self.offset)
        whole = data[: data.rfind(b"\n") + 1]
        for line in whole.splitlines():
            if line.strip():
                try:
                    self.step(json.loads(line))
                except (ValueError, KeyError) as exc:
                    raise store.StoreError(f"{path}: a line after byte {self.offset} cannot be read") from exc
        self.offset += len(whole)


class _Cost(_Fold):
    """Every call's cost by day and family, agent and model, and in total by agent, task and model. Each total is
    [dollars, calls with unknown price]: an unknown price is counted, never added as zero."""

    def reset(self) -> None:
        self.days: dict[str, dict[str, dict[str, list[float]]]] = {}
        self.totals: dict[str, dict[str, list[float]]] = {"agent": {}, "task": {}, "model": {}}

    def step(self, raw: dict[str, Any]) -> None:
        record = models.load(models.CostRecord, raw)
        agent = record.subagent or record.session
        day = self.days.setdefault(record.at[:10], {"family": {}, "agent": {}, "model": {}})
        cells = [
            day["family"].setdefault(record.family, [0.0, 0]),
            day["agent"].setdefault(agent, [0.0, 0]),
            day["model"].setdefault(record.model, [0.0, 0]),
            self.totals["agent"].setdefault(agent, [0.0, 0]),
            self.totals["task"].setdefault(record.task, [0.0, 0]),
            self.totals["model"].setdefault(record.model, [0.0, 0]),
        ]
        for cell in cells:
            cell[0 if record.usd is not None else 1] += record.usd if record.usd is not None else 1

    def answer(self, days: int) -> dict[str, Any]:
        return {"days": {d: self.days[d] for d in sorted(self.days)[-days:]}, "totals": self.totals}


class _Streaks(_Fold):
    """Since when each environment is healthy, or down by a counted failure; an uncounted failure changes nothing."""

    def reset(self) -> None:
        self.state: dict[str, dict[str, Any]] = {}

    def step(self, raw: dict[str, Any]) -> None:
        probe = models.load(models.Probe, raw)
        if (probe.ok or probe.counts) and self.state.get(probe.env, {}).get("ok") is not probe.ok:
            self.state[probe.env] = {"ok": probe.ok, "since": probe.at}


class _Refusals(_Fold):
    """The guard's refusals, by day and rule."""

    def reset(self) -> None:
        self.days: dict[str, dict[str, int]] = {}

    def step(self, raw: dict[str, Any]) -> None:
        day = self.days.setdefault(str(raw["at"])[:10], {})
        day[str(raw["rule"])] = day.get(str(raw["rule"]), 0) + 1


class _Deployments:
    """Each environment's deployments on GitHub, refreshed in the background so no poll waits for GitHub."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.fetched = 0.0
        self.running = False
        self.found: dict[str, Any] = {}

    def get(self, ctx: Context) -> dict[str, Any]:
        with self.lock:
            due = ctx.clock.now() - self.fetched >= ctx.settings().get_int("watch.probe_seconds")
            if due and not self.running:
                self.running = True
                threading.Thread(target=self._refresh, args=(ctx,), name="deployments", daemon=True).start()
            return dict(self.found)

    def _refresh(self, ctx: Context) -> None:
        found: dict[str, Any] = {}
        for env in ENVIRONMENTS:
            try:
                found[env] = ctx.github.deployment(env)
            # telemetry: surfaced — the dashboard draws the deployment as unknown, with GitHub's reason
            except GitHubError as exc:
                found[env] = {"error": str(exc)}
            except Exception as exc:
                log.exception("warden: reading the %s deployments failed (%s)", env, type(exc).__name__)
                found[env] = {"error": f"the warden failed reading them ({type(exc).__name__})"}
        with self.lock:
            self.found, self.fetched, self.running = found, ctx.clock.now(), False


_folds: dict[tuple[str, str], Any] = {}
_folds_lock = threading.Lock()
_deployments: dict[str, _Deployments] = {}


def _kept(kind: type[Any], path: Path) -> Any:
    with _folds_lock:
        return _folds.setdefault((kind.__name__, str(path)), kind())


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
    found = {}
    for sid, s in registry.read(ctx.places).items():
        row = models.dump(s)
        try:
            row["facts"] = doing.facts(ctx.places, sid)
            row["sentence"] = doing.sentence(current, s, row["facts"]) if s.state != RETIRED else s.doing
        # telemetry: surfaced — the agent's sentence draws as unknown; the rest of the tree still draws
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
            # telemetry: surfaced — a broken spec is a row that says so; the queue-format sweep files it
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
        ready = store.read_json_or(ctx.places.state / "public" / "ready" / f"{s.id}.json", {"records": []})
        found[s.task]["ready"] += ready.get("records", []) if isinstance(ready, dict) else []
        found[s.task]["results"] += store.read_jsonl(ctx.places.state / "qa-results" / f"{s.id}.jsonl")
    for entry in store.read_jsonl(ctx.places.evidence / "index.jsonl"):
        if entry.get("task") in wanted:
            found[entry["task"]]["evidence"].append(entry)
    return found


def _probes(ctx: Context, args: dict[str, Any]) -> Any:
    strip = ctx.settings().get_int("dashboard.probe_strip")
    streaks = _kept(_Streaks, ctx.places.probes)
    streaks.read(ctx.places.probes)
    return {
        "recent": {env: [models.dump(p) for p in probes.recent(ctx.places, env, strip)] for env in probes.ENVS},
        "streak": dict(streaks.state),
    }


def _cost(ctx: Context, args: dict[str, Any]) -> Any:
    days = args.get("days", 1)
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 366:
        raise Refused("snapshot", "days is a whole number from 1 to 366")
    book = _kept(_Cost, ctx.places.cost)
    book.read(ctx.places.cost)
    # A copy: the answer is sent after the lock is let go, while the next snapshot may fold new calls in.
    return copy.deepcopy(book.answer(days))


def _refusals(ctx: Context, args: dict[str, Any]) -> Any:
    tally = _kept(_Refusals, ctx.places.refusals)
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
        "items": [{"id": i.id, "text": i.text} for i in items],
        "text": raw.decode(),
        "changed": (repo / goal.GOAL_FILE).stat().st_mtime,
    }


def _memory() -> int | None:
    """The laptop's memory in kB, so free memory can be drawn as a share of it."""
    try:
        found = re.search(r"^MemTotal:\s+(\d+) kB", Path("/proc/meminfo").read_text(), re.MULTILINE)
    # telemetry: surfaced — the memory bar draws as unknown
    except OSError:
        return None
    return int(found.group(1)) if found else None


def _factory(ctx: Context, args: dict[str, Any]) -> Any:
    paused = store.read_json_or(paused_path(ctx), None)
    until = codes.locked_until(ctx.places, ctx.clock.now())
    triage = store.read_json_or(ctx.places.state / "triage-last.json", None)
    return {"paused": paused, "locked_until": until, "triage": triage}


READERS: dict[str, Reader] = {
    "settings": _settings,
    "sessions": _sessions,
    "proposals": lambda ctx, args: (lambda f: {"open": f.open, "approved": f.approved})(proposals.read(ctx.places)),
    "cards": lambda ctx, args: [models.dump(c) for c in cards.read(ctx.places)],
    "outage": lambda ctx, args: (lambda o: None if o is None else models.dump(o))(outage.read(ctx.places)),
    "factory": _factory,
    "probes": _probes,
    "plan": lambda ctx, args: plan.reading(ctx),
    "vitals": lambda ctx, args: {"reading": store.read_json_or(ctx.places.vitals, None), "memory": _memory()},
    "rules": lambda ctx, args: {"rules": RULES, "off": sorted(public.read_switches(ctx.places))},
    "refusals": _refusals,
    "slots": _slots,
    "queue": _queue,
    "logs": _logs,
    "specs": _spec_text,
    "qa": _qa,
    "goal": _goal,
    "docker": lambda ctx, args: store.read_json_or(gc.disk_path(ctx.places), None),
    "reflections": lambda ctx, args: reflections.entries(ctx.places),
    "cost": _cost,
}


def _read_one(ctx: Context, name: str, args: dict[str, Any]) -> tuple[Any, str | None]:
    try:
        return READERS[name](ctx, args), None
    # telemetry: surfaced — the part that needs it draws the reason in its place
    except Refused as exc:
        return None, exc.reason
    except (store.StoreError, models.RecordError, ValueError, OSError) as exc:
        log.warning("warden: the snapshot could not read %s (%s)", name, type(exc).__name__)
        return None, f"The warden could not read it: {exc}"


def snapshot(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """{"want": [names], "tasks"?: [ids], "specs"?: [ids], "days"?: n} → each name's state, or its error."""
    want = args.get("want")
    if not isinstance(want, list) or not want or not all(isinstance(w, str) for w in want):
        raise Refused("snapshot", f"want is a list of: {', '.join(sorted([*READERS, 'deployments']))}")
    unknown = sorted(set(want) - set(READERS) - {"deployments"})
    if unknown:
        raise Refused("snapshot", f"no such state: {', '.join(unknown)}")
    state: dict[str, Any] = {}
    errors: dict[str, str] = {}
    with ctx.lock:
        now = ctx.clock.now()
        for name in dict.fromkeys(want):
            if name == "deployments":
                continue
            state[name], error = _read_one(ctx, name, args)
            if error is not None:
                errors[name] = error
                del state[name]
    if "deployments" in want:
        with _folds_lock:
            keeper = _deployments.setdefault(str(ctx.places.state), _Deployments())
        state["deployments"] = keeper.get(ctx)
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
    # telemetry: surfaced — the page draws "The picture could not be shown."
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
    fd = -1
    try:
        fd = os.open(ctx.places.evidence, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        for part in [task, *parts[:-1]]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = nxt
        leaf = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
    # telemetry: surfaced — the page draws "The picture could not be shown."
    except FileNotFoundError as exc:
        raise Refused("evidence", f"{task} has no evidence {rel}; it may have aged out") from exc
    # telemetry: surfaced — a link or a folder where an image should be is refused, never followed
    except OSError as exc:
        raise Refused("evidence", f"{rel} is not a plain file in {task}'s evidence") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    with os.fdopen(leaf, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise Refused("evidence", f"{rel} is not a plain file in {task}'s evidence")
        raw = fh.read(_MAX_IMAGE + 1)
    if len(raw) > _MAX_IMAGE:
        raise Refused("evidence", f"{rel} is too large to show here")
    return {"type": media, "bytes_b64": base64.b64encode(raw).decode()}
