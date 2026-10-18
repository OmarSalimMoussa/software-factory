"""The clean-up, every ``gc.every_hours``: QA evidence past ``qa.evidence_days`` and the testers' runs that old that
nothing took into it, then Docker's build cache and old unused slot images, keeping this machine's Docker data under
``gc.docker_budget_gib``. Last, it measures what fills the disk, by kind, for the disk vital's detail.

The testers' runs are the agent user's, so they are removed through the agent's unit. The stack user's runs reach
Docker, so both Docker verbs run through the stack unit. Prod shares that Docker, so only images a slot labelled are
pruned. Volumes are never pruned: the shared services keep theirs, and a reap removes a slot's own. The warden
measures the agent's folders itself, as it reads their transcripts, at the lowest priority.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runtime.qa import qa_browser
from runtime.qa.qa_browser import RUN, STAMP
from runtime.queue import formats
from runtime.shared import store
from runtime.shared.paths import Places
from runtime.slots import leases
from runtime.warden.context import Context
from runtime.warden.owner import reflections
from runtime.warden.sessions import launch, session_settings

log = logging.getLogger("factory.warden")

# A slot number for the stack unit's request; the prune and the reading are not about any one slot.
_NO_SLOT = 0
_SESSION = "gc"
# The lowest CPU priority and the idle I/O class: measuring the disk never slows the agents or prod.
_IDLE = ["nice", "-n", "19", "ionice", "-c", "3"]
KINDS = ("worktrees", "docker", "qa", "transcripts")


def record_path(places: Places) -> Path:
    """The last clean-up: started, ended (None while it runs), and the disk's ``use`` by kind when ``measured``."""
    return places.state / "gc.json"


def record(places: Places) -> dict[str, Any] | None:
    found: dict[str, Any] | None = store.read_json_or(record_path(places), None)
    return found


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


def _docker_disk(ctx: Context) -> int | None:
    done = ctx.launcher.run_stack(_NO_SLOT, [leases.DISK], _SESSION)
    last = (done.tail.strip().splitlines() or [""])[-1].strip()
    if done.exit != 0 or not last.isdigit():
        log.warning("warden: the Docker disk reading failed (exit %d)", done.exit)
        return None
    return int(last)


def prune_docker(ctx: Context) -> int | None:
    """Prune Docker; returns its data's size after, or None when it could not be read."""
    settings = ctx.settings()
    budget = settings.get_int("gc.docker_budget_gib") * 1024**3
    used: int | None = None
    # Old images first; over the budget after that, every unused image.
    for hours in (settings.get_int("gc.images_keep_days") * 24, 0):
        done = ctx.launcher.run_stack(_NO_SLOT, [leases.PRUNE, str(hours)], _SESSION)
        if done.exit != 0:
            log.warning("warden: the Docker prune failed (exit %d)", done.exit)
        used = _docker_disk(ctx)
        if used is None or used <= budget:
            break
    if used is not None and used > budget:
        reflections.incident(ctx, f"Docker data is {used / 1024**3:.1f} GiB after the clean-up, over its "
                                  f"{budget // 1024**3} GiB budget.")  # fmt: skip
    return used


def _du(folders: list[Path], timeout: int) -> dict[str, int]:
    """Each folder's bytes on its own filesystem, by path; one du could not read is left out."""
    argv = [*_IDLE, "du", "--summarize", "--one-file-system", "--block-size=1", "--", *map(str, folders)]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("warden: measuring what fills the disk failed (%s)", type(exc).__name__)
        return {}
    if done.returncode != 0:
        log.warning("warden: measuring what fills the disk failed (exit %d)", done.returncode)
    rows = (line.partition("\t") for line in done.stdout.splitlines())
    return {path: int(size) for size, _, path in rows if size.isdigit()}


def measure(ctx: Context, docker: int | None) -> dict[str, int | None]:
    """What fills the disk by kind, in bytes: None for a kind not read, and nothing in a folder not made yet."""
    repo = Path(ctx.settings().get_str("repo.path"))
    kinds = {
        "worktrees": [session_settings.worktrees(repo)],
        "qa": [qa_browser.runs_root(), ctx.places.evidence],
        "transcripts": [launch.transcripts(launch.agent_home())],
    }
    there = [folder for folders in kinds.values() for folder in folders if folder.is_dir()]
    sizes = _du(there, ctx.settings().get_int("factory.command_seconds")) if there else {}
    use: dict[str, int | None] = {"docker": docker}
    for kind, folders in kinds.items():
        found = [sizes.get(str(f)) if f in there else 0 for f in folders]
        use[kind] = None if None in found else sum(n for n in found if n is not None)
    return use


def clean(ctx: Context) -> dict[str, Any]:
    settings, started = ctx.settings(), ctx.clock.iso()
    prune_evidence(ctx.places, ctx.clock.now(), settings.get_int("qa.evidence_days"))
    prune_runs(ctx, qa_browser.runs_root(), settings.get_int("qa.evidence_days"))
    use = measure(ctx, prune_docker(ctx))
    done = {"started": started, "ended": ctx.clock.iso(), "use": use, "measured": ctx.clock.iso()}
    store.write_json(record_path(ctx.places), done)
    return done


class Cleanup:
    """Starts the clean-up every gc.every_hours, in a thread of its own."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None

    def run_pass(self, ctx: Context) -> None:
        last = record(ctx.places)
        every = ctx.settings().get_int("gc.every_hours") * 3600
        if last is not None and ctx.clock.now() - formats.epoch(last["started"]) < every:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        # Recorded before it runs, so one that fails is not started again every tick; what it measured last stays.
        kept = last if last is not None else {"use": dict.fromkeys(KINDS), "measured": None}
        store.write_json(record_path(ctx.places), {**kept, "started": ctx.clock.iso(), "ended": None})
        self._thread = threading.Thread(target=self._run, args=(ctx,), name="gc", daemon=True)
        self._thread.start()

    def _run(self, ctx: Context) -> None:
        try:
            clean(ctx)
        except Exception as exc:
            log.exception("warden: the clean-up failed (%s)", type(exc).__name__)
