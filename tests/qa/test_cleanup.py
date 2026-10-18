"""The clean-up runs every gc.every_hours in a thread of its own, and measures what fills the disk, by kind."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import pytest
from queue_kit import World

from runtime.qa import qa_browser
from runtime.shared import store
from runtime.warden import gc
from runtime.warden.context import Context
from runtime.warden.sessions import launch

MIB = 1024**2
USE = {"worktrees": 1, "docker": 2, "qa": 3, "transcripts": 4}


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The agent's home, where its transcripts and its testers' runs are."""
    found = tmp_path / "agent"
    monkeypatch.setattr(launch, "agent_home", lambda: found)
    monkeypatch.setattr(qa_browser, "runs_root", lambda: found / ".factory-qa")
    return found


def _file(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(os.urandom(size))  # random, so no filesystem stores it in less


def test_what_fills_the_disk_is_measured_by_kind_and_a_folder_not_made_yet_holds_nothing(
    world: World, home: Path
) -> None:
    _file(world.repo / ".claude" / "worktrees" / "impl-t-0001" / "node_modules" / "big.js", 2 * MIB)
    _file(world.places.evidence / "t-0001" / "shot.png", MIB // 4)
    _file(home / ".factory-qa" / "s-1" / "run" / "frame.jpeg", MIB // 4)

    use = gc.measure(world.ctx(), 5 * 1024**3)

    assert use["docker"] == 5 * 1024**3 and use["transcripts"] == 0
    assert use["worktrees"] is not None and use["worktrees"] >= 2 * MIB
    assert use["qa"] is not None and MIB // 2 <= use["qa"] < 2 * MIB


def test_a_measure_that_fails_reads_unknown_and_is_reported(
    world: World, home: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    (world.repo / ".claude" / "worktrees").mkdir(parents=True)

    def broken(*args: Any, **kwargs: Any) -> None:
        raise OSError("du is missing")

    monkeypatch.setattr(gc.subprocess, "run", broken)
    with caplog.at_level(logging.WARNING, logger="factory.warden"):
        use = gc.measure(world.ctx(), None)

    assert use == {"docker": None, "worktrees": None, "qa": 0, "transcripts": 0}
    assert "warden: measuring what fills the disk failed (OSError)" in caplog.text


def test_the_clean_up_runs_every_gc_every_hours_and_keeps_what_it_measured_last_while_it_runs(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[str] = []

    def clean(ctx: Context) -> None:
        ran.append(ctx.clock.iso())

    monkeypatch.setattr(gc, "clean", clean)
    last = {"started": "2026-10-09T06:00:00Z", "ended": "2026-10-09T06:01:00Z", "use": USE,
            "measured": "2026-10-09T06:01:00Z"}  # fmt: skip
    store.write_json(gc.record_path(world.places), last)
    cleanup = gc.Cleanup()

    for step in (0, 6 * 3600 - 1, 1):
        world.clock.at += step
        cleanup.run_pass(world.ctx())
        if cleanup._thread is not None:
            cleanup._thread.join()

    assert ran == ["2026-10-09T12:00:00Z", "2026-10-09T18:00:00Z"]
    assert gc.record(world.places) == {**last, "started": "2026-10-09T18:00:00Z", "ended": None}
