"""A finished tester's runs, moved into the warden's evidence (factory spec, "Briefs, time and evidence"): a task's
tester's at its result, by task, head and tester; the regression testers' when the orchestrator saves the check they
walked for, by check, commit and tester. Only the owner reads it there, through the dashboard. Each entry names the
tester's agent, whose transcript the warden's replays pass reads to build the run's replay (replay/); a regression
run no tester is known for names none, so it shows its pictures and has no replay.

The runs sit in a folder the agent user owns, so the copy follows no symlink and takes regular files only; then the
agent's copy is removed through the agent's own unit.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import shutil
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from runtime.qa.qa_browser import RUN, SESSION
from runtime.qa.replay.build import FOLDER as REPLAY
from runtime.queue.book import Queue
from runtime.queue.formats import CHECK_ID, TASK_ID
from runtime.queue.records import CheckRecord
from runtime.shared import lanes, store
from runtime.shared.models import RETIRED, Session
from runtime.shared.paths import Places
from runtime.warden.context import Context
from runtime.warden.sessions import registry
from runtime.warden.verbs import REGRESSION

log = logging.getLogger("factory.warden")

TESTER = re.compile(r"^qa-[a-z0-9-]+$")


def index_path(places: Places) -> Path:
    return places.evidence / "index.jsonl"


def entries(places: Places) -> list[dict[str, Any]]:
    """Every run taken into the evidence, oldest first."""
    return store.read_jsonl(index_path(places))


def of(entry: dict[str, Any]) -> str:
    """The task or the check a run was taken for, as its entry names it: the first folder of its path here."""
    return str(entry["check"] if "check" in entry else entry["task"])


def named(of_id: str) -> bool:
    return bool(TASK_ID.match(of_id) or CHECK_ID.match(of_id))


def folder(places: Places, entry: dict[str, Any]) -> Path:
    return places.evidence / of(entry) / str(entry["head"])[:12] / str(entry["tester"]) / str(entry["run"])


def _copy_tree(source: Path, target: Path) -> list[str]:
    """Regular files under ``source``, copied without following any link; their paths under ``target``. The run's
    ``replay/`` is the warden's to write: one the tester's side left would pass for a replay already built."""
    copied: list[str] = []
    for folder, dirs, names in os.walk(source, followlinks=False):
        here = Path(folder)
        dirs[:] = [d for d in dirs if not (here / d).is_symlink() and not (here == source and d == REPLAY)]
        for name in names:
            src = here / name
            try:
                fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    continue
                raise
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    continue
                rel = src.relative_to(source)
                (target / rel).parent.mkdir(parents=True, exist_ok=True)
                with os.fdopen(os.dup(fd), "rb") as reader, (target / rel).open("wb") as writer:
                    shutil.copyfileobj(reader, writer)
                copied.append(str(rel))
            finally:
                os.close(fd)
    return copied


def _untaken(places: Places, runs: Path, session_id: str) -> Iterator[tuple[Path, dict[str, Any]]]:
    """Each run a session's testers left that the evidence does not hold yet, with what its run.json says."""
    taken = {(e["session"], e["run"]) for e in entries(places)}
    left = runs / session_id
    for run in sorted(left.iterdir()) if left.is_dir() and not left.is_symlink() else []:
        if RUN.match(run.name) and not run.is_symlink() and (session_id, run.name) not in taken:
            yield run, _meta(run)


def _take(ctx: Context, runs: Path, run: Path, entry: dict[str, Any]) -> None:
    files = _copy_tree(run, folder(ctx.places, entry))
    store.append_jsonl(index_path(ctx.places), {**entry, "at": ctx.clock.iso(), "files": files})
    code, _ = ctx.launcher.agent_command(["rm", "-rf", "--", str(run)], str(runs))
    if code != 0:
        log.warning("warden: the agent's copy of QA run %s could not be removed (exit %d)", run.name, code)


def intake(
    ctx: Context, runs: Path, session_id: str, task: str, head: str, started_as: str, agent: str, since: str
) -> list[str]:
    """Take the runs the subagent ``agent``, started as ``started_as``, made in the session since ``since``."""
    if not SESSION.match(session_id) or not TESTER.match(started_as):
        raise ValueError("evidence is taken for a Claude Code session and a tester kind")
    found: list[str] = []
    for run, meta in _untaken(ctx.places, runs, session_id):
        if meta.get("role") != started_as or str(meta.get("started", "")) < since:
            continue
        names = {"task": task, "head": head, "tester": lanes.kind_of(started_as), "run": run.name}
        _take(ctx, runs, run, {**names, "session": session_id, "agent": agent})
        found.append(run.name)
    return found


def intake_check(ctx: Context, runs: Path, check: CheckRecord) -> list[str]:
    """Take the runs the orchestrator's regression testers made, as itself or as the session it copied, whose walk
    ended since the check before ``check``. A run is the tester's that walked its lane as it started (the guard lets
    one at a time). A run waits while its tester walks, or, with no tester known, while its lane has one."""
    since = max((c.at for c in Queue.of(ctx).checks()[0] if c.id != check.id), default=None)
    sessions = registry.read(ctx.places)
    me = sessions.get(ctx.caller.session)
    testers = [s for s in sessions.values() if s.parent == ctx.caller.session and s.role == REGRESSION]
    walking = {s.started_as for s in testers if s.state != RETIRED}
    found: list[str] = []
    for session_id in dict.fromkeys([ctx.caller.session, me.copied_from if me else ""]):
        for run, meta in _untaken(ctx.places, runs, session_id) if SESSION.match(session_id) else ():
            lane, started = str(meta.get("role", "")), str(meta.get("started", ""))
            tester = _walker(testers, lane, started)
            waits = tester.state != RETIRED if tester else lane in walking
            ended = tester.ended if tester else started
            if lanes.kind_of(lane) != REGRESSION or waits or (since is not None and ended < since):
                continue
            names = {"check": check.id, "head": check.commit, "tester": REGRESSION, "run": run.name}
            _take(ctx, runs, run, {**names, "session": session_id, "agent": tester.agent_id if tester else None})
            found.append(run.name)
    return found


def _walker(testers: list[Session], lane: str, started: str) -> Session | None:
    """The tester that was running in ``lane`` at ``started``, when a run's browser started."""
    running = [
        t for t in testers if t.started_as == lane and t.started <= started and (not t.ended or started <= t.ended)
    ]
    return max(running, key=lambda t: t.started) if running else None


class TooLarge(OSError):
    """An evidence file is larger than the caller can carry."""


def read_file(places: Places, parts: list[str], limit: int) -> bytes:
    """One plain file under the evidence, its path given in ``parts`` from the task's folder down, opened folder by
    folder without following any link. FileNotFoundError when it is gone, TooLarge past ``limit`` bytes, and OSError
    when anything on the way is not a plain folder or file."""
    fd = -1
    try:
        fd = os.open(places.evidence, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        for part in parts[:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = nxt
        leaf = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
    finally:
        if fd >= 0:
            os.close(fd)
    with os.fdopen(leaf, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise OSError(f"{'/'.join(parts)} is not a plain file")
        raw = fh.read(limit + 1)
    if len(raw) > limit:
        raise TooLarge(f"{'/'.join(parts)} is larger than {limit} bytes")
    return raw


def _meta(run: Path) -> dict[str, Any]:
    try:
        fd = os.open(run / "run.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    # telemetry: expected — a run with no plain record of its own reads as no facts; its pictures still show
    except OSError:
        return {}
    with os.fdopen(fd) as fh:
        try:
            raw = json.loads(fh.read(65536))
        # telemetry: expected — a record that is not JSON reads as no facts; the run's pictures still show
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}
