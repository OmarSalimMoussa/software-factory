"""The nightly prune, at ``gc.prune_hour``: QA evidence past ``qa.evidence_days`` and the testers' runs that old
that nothing took into it, then Docker's build cache and old unused slot images, keeping this machine's Docker data
under ``gc.docker_budget_gib``.

The testers' runs are the agent user's, so they are removed through the agent's unit. The stack user's runs reach
Docker, so both Docker verbs run through the stack unit. Prod shares that Docker, so only images a slot labelled are
pruned. Volumes are never pruned: the shared services keep theirs, and a reap removes a slot's own. The disk reading
is kept for the Guard page.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import UTC, datetime
from pathlib import Path

from runtime.qa import qa_browser
from runtime.qa.qa_browser import RUN, STAMP
from runtime.shared import store
from runtime.shared.paths import Places
from runtime.slots import leases
from runtime.warden.context import Context
from runtime.warden.owner import reflections

log = logging.getLogger("factory.warden")

# A slot number for the stack unit's request; the prune and the reading are not about any one slot.
_NO_SLOT = 0
_SESSION = "gc"


def _state_path(places: Places) -> Path:
    return places.state / "gc.json"


def disk_path(places: Places) -> Path:
    """The last Docker disk reading: at, bytes (None when unknown), budget_bytes."""
    return places.state / "docker-disk.json"


def prune_evidence(places: Places, now: float, days: int) -> int:
    """Remove evidence files older than ``days``, and the folders that leaves empty; returns how many went."""
    removed = 0
    for folder, _, files in os.walk(places.evidence, topdown=False):
        for name in files:
            path = Path(folder) / name
            if now - path.lstat().st_mtime > days * 86400:
                path.unlink()
                removed += 1
        if Path(folder) != places.evidence and not any(Path(folder).iterdir()):
            Path(folder).rmdir()
    return removed


def prune_runs(ctx: Context, root: Path, days: int) -> int:
    """Remove the runs in the agent's ``root`` that nothing took, past ``days``, and emptied folders; how many went."""
    cutoff = ctx.clock.now() - days * 86400
    gone = 0
    for session in sorted(root.iterdir()) if root.is_dir() else []:
        if session.is_symlink() or not session.is_dir():
            continue
        names = [p.name for p in session.iterdir()]
        old = [n for n in names if (m := RUN.match(n)) and _started(m[1]) < cutoff]
        if old and _agent(ctx, root, ["rm", "-rf", "--", *(str(session / n) for n in old)]):
            gone += len(old)
        if len(old) == len(names):
            # A tester may start a run there meanwhile: rmdir leaves a folder that is not empty, and says nothing.
            _agent(ctx, root, ["rmdir", "--ignore-fail-on-non-empty", "--", str(session)])
    return gone


def _agent(ctx: Context, root: Path, argv: list[str]) -> bool:
    code, _ = ctx.launcher.agent_command(argv, str(root))
    if code != 0:
        log.warning("warden: %s of old QA runs failed (exit %d)", argv[0], code)
    return code == 0


def _started(stamp: str) -> float:
    return datetime.strptime(stamp, STAMP).replace(tzinfo=UTC).timestamp()


def _disk(ctx: Context) -> int | None:
    done = ctx.launcher.run_stack(_NO_SLOT, [leases.DISK], _SESSION)
    last = (done.tail.strip().splitlines() or [""])[-1].strip()
    if done.exit != 0 or not last.isdigit():
        log.warning("warden: the Docker disk reading failed (exit %d)", done.exit)
        return None
    return int(last)


def prune(ctx: Context) -> int | None:
    """One prune; returns the Docker data's size after it, or None when it could not be read."""
    settings = ctx.settings()
    prune_evidence(ctx.places, ctx.clock.now(), settings.get_int("qa.evidence_days"))
    prune_runs(ctx, qa_browser.runs_root(), settings.get_int("qa.evidence_days"))
    budget = settings.get_int("gc.docker_budget_gib") * 1024**3
    used: int | None = None
    # Old images first; over the budget after that, every unused image.
    for hours in (settings.get_int("gc.images_keep_days") * 24, 0):
        done = ctx.launcher.run_stack(_NO_SLOT, [leases.PRUNE, str(hours)], _SESSION)
        if done.exit != 0:
            log.warning("warden: the Docker prune failed (exit %d)", done.exit)
        used = _disk(ctx)
        if used is None or used <= budget:
            break
    store.write_json(disk_path(ctx.places), {"at": ctx.clock.iso(), "bytes": used, "budget_bytes": budget})
    if used is not None and used > budget:
        reflections.incident(ctx, f"Docker data is {used / 1024**3:.1f} GiB after the nightly prune, over its "
                                  f"{budget // 1024**3} GiB budget.")  # fmt: skip
    return used


class Nightly:
    """Starts the prune once a day in the prune hour (local time), in a thread of its own."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None

    def run_pass(self, ctx: Context) -> None:
        now = datetime.fromtimestamp(ctx.clock.now()).astimezone()
        if now.hour != ctx.settings().get_int("gc.prune_hour"):
            return
        today = now.strftime("%Y-%m-%d")
        if store.read_json_or(_state_path(ctx.places), {}).get("last_day") == today:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        # Recorded before the prune runs, so a prune that fails is not started again every tick of the hour.
        store.write_json(_state_path(ctx.places), {"last_day": today}, mode=0o600)
        self._thread = threading.Thread(target=self._run, args=(ctx,), name="gc", daemon=True)
        self._thread.start()

    def _run(self, ctx: Context) -> None:
        try:
            prune(ctx)
        except Exception as exc:
            log.exception("warden: the nightly prune failed (%s)", type(exc).__name__)
