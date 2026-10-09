"""A finished tester's runs, moved into the warden's evidence, indexed by task, head and tester (factory spec,
"Briefs, time and evidence"). Only the owner reads it there, through the dashboard.

The runs sit in a folder the agent user owns, so the copy follows no symlink and takes regular files only; then the
agent's copy is removed through the agent's own unit.
"""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
import stat
from pathlib import Path
from typing import Any

from runtime.qa.qa_browser import RUN, SESSION
from runtime.shared import store
from runtime.shared.paths import Places
from runtime.warden.context import Context

TESTER = re.compile(r"^qa-[a-z-]+$")


def index_path(places: Places) -> Path:
    return places.evidence / "index.jsonl"


def _copy_tree(source: Path, target: Path) -> list[str]:
    """Regular files under ``source``, copied without following any link; their paths under ``target``."""
    copied: list[str] = []
    for folder, dirs, names in os.walk(source, followlinks=False):
        here = Path(folder)
        dirs[:] = [d for d in dirs if not (here / d).is_symlink()]
        for name in names:
            src = here / name
            try:
                fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
            # telemetry: expected — a link in a run is never followed, so it is left behind
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


def intake(ctx: Context, runs: Path, session_id: str, task: str, head: str, tester: str, since: str) -> list[str]:
    """Take every run of ``tester`` in the session since ``since``; the runs taken."""
    if not SESSION.match(session_id) or not TESTER.match(tester):
        raise ValueError("evidence is taken for a Claude Code session and a tester kind")
    taken = {(e["session"], e["run"]) for e in store.read_jsonl(index_path(ctx.places))}
    found: list[str] = []
    folder = runs / session_id
    for run in sorted(folder.iterdir()) if folder.is_dir() and not folder.is_symlink() else []:
        if not RUN.match(run.name) or run.is_symlink() or (session_id, run.name) in taken:
            continue
        meta = _meta(run)
        if meta.get("role") != tester or str(meta.get("started", "")) < since:
            continue
        target = ctx.places.evidence / task / head[:12] / tester / run.name
        files = _copy_tree(run, target)
        store.append_jsonl(
            index_path(ctx.places),
            {
                "task": task,
                "head": head,
                "tester": tester,
                "run": run.name,
                "session": session_id,
                "at": ctx.clock.iso(),
                "files": files,
            },
        )
        ctx.launcher.agent_command(["rm", "-rf", "--", str(run)], str(runs))
        found.append(run.name)
    return found


def _meta(run: Path) -> dict[str, Any]:
    try:
        fd = os.open(run / "run.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError:
        return {}
    with os.fdopen(fd) as fh:
        try:
            raw = json.loads(fh.read(65536))
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}
