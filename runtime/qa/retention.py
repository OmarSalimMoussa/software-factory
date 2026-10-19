"""QA kept for the newest merge bundles, ``qa.keep_bundles`` of them, and what the clean-up removes past them.

A bundle is a saved check that cleared at least one merge. The warden moves each task a check clears to checked in the
request that saved the check, so the check that cleared a task is the newest one saved at or before that move. Past
the bundles kept, three things go: the QA evidence of a settled task (checked or later, or closed) that settled before
the oldest bundle kept and that no live session works on; the regression runs of a check saved before it; and the
runs nothing took into the evidence, started before it in a session that is neither live nor run on as a live copy.
While fewer bundles have cleared, nothing goes. Each task or check whose evidence went is recorded, so the dashboard
says it was cleaned up instead of showing nothing. Transcripts are never touched.
"""

from __future__ import annotations

import bisect
import logging
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from runtime.qa.qa_browser import RUN, STAMP
from runtime.queue import tasks, turn
from runtime.queue.book import Queue
from runtime.queue.records import CheckRecord
from runtime.shared import store
from runtime.shared.models import RETIRED, Task
from runtime.shared.paths import Places
from runtime.warden.context import Context
from runtime.warden.sessions import registry

log = logging.getLogger("factory.warden")
_ISO = "%Y-%m-%dT%H:%M:%SZ"


def cleaned_path(places: Places) -> Path:
    return places.evidence / "cleaned.json"


def cleaned(places: Places) -> dict[str, str]:
    """When the evidence of each task or check went, by its id."""
    found: dict[str, str] = store.read_json_or(cleaned_path(places), {})
    return found


def why_gone(ctx: Context, of: str) -> str | None:
    at = cleaned(ctx.places).get(of)
    if at is None:
        return None
    keep = ctx.settings().get_int("qa.keep_bundles")
    return f"the QA of {of} was cleaned up on {at[:10]}: the factory keeps that of the newest {keep} merge bundles"


def oldest_kept(checks: list[CheckRecord], moves: list[str], keep: int) -> str | None:
    """When the oldest of the ``keep`` newest bundles was saved, from when each task moved to checked; None while
    fewer have cleared."""
    saved = sorted(c.at for c in checks)
    bundles = sorted({saved[n - 1] for at in moves if (n := bisect.bisect_right(saved, at))}, reverse=True)
    return bundles[keep - 1] if len(bundles) >= keep else None


def _settled(task: Task, log_rows: list[dict[str, str]]) -> str | None:
    if task.status not in tasks.SETTLED:
        return None
    closed = task.status == "closed"
    found = [e["at"] for e in log_rows if (e.get("to") == "closed" if closed else e["event"] == turn.CHECKED)]
    return max(found, default=None)


@dataclass(frozen=True, slots=True)
class Past:
    evidence: list[str]
    runs: list[Path]


def past(ctx: Context, runs: Path) -> Past:
    queue = Queue.of(ctx)
    found, checks = queue.tasks()[0], queue.checks()[0]
    logs = {t.id: queue.read_log(t.id) for t in found}
    moves = [e["at"] for rows in logs.values() for e in rows if e["event"] == turn.CHECKED]
    since = oldest_kept(checks, moves, ctx.settings().get_int("qa.keep_bundles"))
    if since is None:
        return Past([], [])
    live = [s for s in registry.read(ctx.places).values() if s.state != RETIRED]
    busy = {s.task for s in live}
    when = {t.id: at for t in found if t.id not in busy and (at := _settled(t, logs[t.id])) is not None}
    when.update({c.id: c.at for c in checks})
    folders = sorted(ctx.places.evidence.iterdir()) if ctx.places.evidence.is_dir() else []
    old = [p.name for p in folders if p.is_dir() and not p.is_symlink() and p.name in when and when[p.name] < since]
    kept = {s.id for s in live} | {s.copied_from for s in live if s.copied_from}
    left: list[Path] = []
    for session in sorted(runs.iterdir()) if runs.is_dir() else []:
        if session.name in kept or session.is_symlink() or not session.is_dir():
            continue
        left += [r for r in sorted(session.iterdir()) if (m := RUN.match(r.name)) and _iso(m[1]) < since]
    return Past(old, left)


def _iso(stamp: str) -> str:
    return datetime.strptime(stamp, STAMP).strftime(_ISO)


def remove(ctx: Context, runs: Path) -> tuple[int, int, list[str]]:
    """Remove what the newest bundles do not keep: how many tasks' and checks' evidence and runs went, and what
    failed, in words. The runs are the agent user's, so they go through its unit, a session's folder at once when
    nothing else is in it."""
    found, gone, failed = past(ctx, runs), {}, []
    for of in found.evidence:
        try:
            shutil.rmtree(ctx.places.evidence / of)
        except OSError as exc:
            log.warning("warden: the clean-up could not remove the QA of %s (%s)", of, type(exc).__name__)
            failed.append(f"the QA of {of} could not be removed ({type(exc).__name__})")
        else:
            gone[of] = ctx.clock.iso()
    if gone:
        store.write_json(cleaned_path(ctx.places), {**cleaned(ctx.places), **gone})
    removed = 0
    for session in sorted({r.parent for r in found.runs}):
        stale = [r for r in found.runs if r.parent == session]
        whole = sorted(p.name for p in session.iterdir()) == [r.name for r in stale]
        for target in [session] if whole else stale:
            code, _ = ctx.launcher.agent_command(["rm", "-rf", "--", str(target)], str(runs))
            if code != 0:
                log.warning("warden: the clean-up could not remove the runs in %s (exit %d)", target.name, code)
                failed.append(f"the runs in {target.name} could not be removed (exit {code})")
            else:
                removed += 1 if target != session else len(stale)
    return len(gone), removed, failed
