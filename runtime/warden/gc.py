"""The clean-up, every ``gc.every_hours``: the QA past the merge bundles kept (runtime/qa/retention.py), the worktrees
held for a task that has since settled, stack logs past ``gc.logs_keep_days``, then Docker's build cache unused for
``gc.build_cache_keep_hours`` and unused slot images past ``gc.images_keep_days``, keeping this machine's Docker data
under ``gc.docker_budget_gib``. Last, it measures what fills the disk, by kind, for the disk vital's detail. What it
removed is logged. What failed is logged and recorded, and the disk vital turns amber until a clean-up passes; so it
does while one runs past the next one's time. Transcripts are never touched.

The stack user's runs reach Docker, so both Docker verbs run through the stack unit. Prod shares that Docker, so only
images a slot labelled are pruned. Volumes are never pruned: the shared services keep theirs, and a reap removes a
slot's own. The warden measures the agent's folders itself, as it reads their transcripts, at the lowest priority.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from pathlib import Path
from typing import Any

from runtime.qa import qa_browser, retention
from runtime.queue import formats
from runtime.shared import store
from runtime.shared.paths import Places
from runtime.shared.settings import Settings
from runtime.slots import leases
from runtime.warden.context import Context
from runtime.warden.owner import reflections
from runtime.warden.sessions import launch, reap, session_settings

log = logging.getLogger("factory.warden")

# A slot number for the stack unit's request; the prune and the reading are not about any one slot.
_NO_SLOT = 0
_SESSION = "gc"
# The lowest CPU priority and the idle I/O class: measuring the disk never slows the agents or prod.
_IDLE = ["nice", "-n", "19", "ionice", "-c", "3"]
KINDS = ("worktrees", "docker", "qa", "transcripts")


def record_path(places: Places) -> Path:
    """The last clean-up: started, ended (None while it runs), what ``failed`` in words, and the disk's ``use`` by
    kind when ``measured``."""
    return places.state / "gc.json"


def record(places: Places) -> dict[str, Any] | None:
    found: dict[str, Any] | None = store.read_json_or(record_path(places), None)
    return found


def problem(found: dict[str, Any] | None, now: float, settings: Settings) -> str | None:
    """What is wrong with the clean-up, in words: the last one failed, or one runs past the next one's time."""
    if found is None:
        return None
    late = now - formats.epoch(found["started"]) >= settings.get_int("gc.every_hours") * 3600
    if found["ended"] is None and late:
        return f"the clean-up started {found['started']} has not ended"
    return f"the last clean-up failed: {'; '.join(found['failed'])}" if found["failed"] else None


def view(ctx: Context) -> dict[str, Any] | None:
    found = record(ctx.places)
    return None if found is None else {**found, "problem": problem(found, ctx.clock.now(), ctx.settings())}


def prune_logs(folder: Path, now: float, days: int) -> int:
    """Remove the files in ``folder`` older than ``days``; how many went."""
    old = [p for p in folder.iterdir() if p.is_file() and now - p.lstat().st_mtime > days * 86400]
    for path in old:
        path.unlink()
    return len(old)


def _docker_disk(ctx: Context) -> int | None:
    done = ctx.launcher.run_stack(_NO_SLOT, [leases.DISK], _SESSION)
    last = (done.tail.strip().splitlines() or [""])[-1].strip()
    if done.exit != 0 or not last.isdigit():
        log.warning("warden: the Docker disk reading failed (exit %d)", done.exit)
        return None
    return int(last)


def prune_docker(ctx: Context) -> tuple[int | None, list[str]]:
    """Prune Docker: its data's size after (None when it could not be read), and what failed, in words."""
    settings = ctx.settings()
    budget = settings.get_int("gc.docker_budget_gib") * 1024**3
    used: int | None = None
    failed = []
    old = (settings.get_int("gc.images_keep_days") * 24, settings.get_int("gc.build_cache_keep_hours"))
    # Old images and cache first; over the budget after that, every unused image and all the cache.
    for ages in (old, (0, 0)):
        done = ctx.launcher.run_stack(_NO_SLOT, [leases.PRUNE, *map(str, ages)], _SESSION)
        if done.exit != 0:
            log.warning("warden: the Docker prune failed (exit %d)", done.exit)
            failed.append(f"the Docker prune failed (exit {done.exit})")
        used = _docker_disk(ctx)
        if used is None or used <= budget:
            break
    if used is not None and used > budget:
        reflections.incident(ctx, f"Docker data is {used / 1024**3:.1f} GiB after the clean-up, over its "
                                  f"{budget // 1024**3} GiB budget.")  # fmt: skip
    return used, failed


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


def clean(ctx: Context) -> None:
    started = ctx.clock.iso()
    logs = ctx.places.stack_logs
    stale = prune_logs(logs, ctx.clock.now(), ctx.settings().get_int("gc.logs_keep_days")) if logs.is_dir() else 0
    qa, runs, failed = retention.remove(ctx, qa_browser.runs_root())
    held = reap.retry_held(ctx)
    docker, refused = prune_docker(ctx)
    log.info(
        "warden: the clean-up removed the QA of %d tasks and checks, %d runs nothing took, %d held worktrees "
        "and %d stack logs",
        qa,
        runs,
        held,
        stale,
    )
    use, ended = measure(ctx, docker), ctx.clock.iso()
    done = {"started": started, "ended": ended, "failed": failed + refused, "use": use, "measured": ended}
    store.write_json(record_path(ctx.places), done)


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
        kept = last if last is not None else {"failed": [], "use": dict.fromkeys(KINDS), "measured": None}
        running = {**kept, "started": ctx.clock.iso(), "ended": None}
        store.write_json(record_path(ctx.places), running)
        self._thread = threading.Thread(target=self._run, args=(ctx, running), name="gc", daemon=True)
        self._thread.start()

    def _run(self, ctx: Context, running: dict[str, Any]) -> None:
        try:
            clean(ctx)
        except Exception as exc:
            log.exception("warden: the clean-up failed (%s)", type(exc).__name__)
            stopped = {"ended": ctx.clock.iso(), "failed": [f"it stopped ({type(exc).__name__})"]}
            store.write_json(record_path(ctx.places), {**running, **stopped})
