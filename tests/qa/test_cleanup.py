"""The clean-up runs every gc.every_hours in a thread of its own. It keeps the QA of the newest merge bundles, removes
worktrees held for tasks that since settled and Docker data past its ages, and measures what fills the disk, by kind.
What it could not do is logged, and turns the disk vital amber."""

from __future__ import annotations

import dataclasses
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from queue_kit import World

from runtime.qa import qa_browser, retention
from runtime.queue import formats, records, turn
from runtime.queue.book import task_rel
from runtime.shared import store
from runtime.shared.models import RETIRED
from runtime.slots import leases
from runtime.warden import gc
from runtime.warden.context import Context, StackResult
from runtime.warden.health import vitals
from runtime.warden.owner import reflections
from runtime.warden.sessions import launch, reap, registry

MIB, GIB = 1024**2, 1024**3
USE = {"worktrees": 1, "docker": 2, "qa": 3, "transcripts": 4}
OLD, NEW = "20261002T090000Z-0a0a0a", "20261008T090000Z-0b0b0b"  # runs started Oct 2 and Oct 8


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


def _check(world: World, day: int) -> str:
    """A check saved at 10:00 on October ``day``, with its QA."""
    at = f"2026-10-0{day}T10:00:00Z"
    rows = [records.ScenarioRow("sign-in", "pass", 3, "x")]
    check = records.CheckRecord(f"c-2026100{day}T100000Z-abc1234", "abc1234" + "0" * 33, at, "Check", rows)
    world.write_raw(f"checks/{check.id}.md", records.dump_check(check))
    _file(world.places.evidence / check.id / ("a" * 12) / "qa-regression" / OLD / "shot.png", 1)
    return check.id


def _task(world: World, status: str, day: int | None = None) -> str:
    """A task at ``status``, which it reached at 10:00 on October ``day``, with its QA."""
    task = world.task()
    world.write_raw(task_rel(task.id), formats.dump_task(dataclasses.replace(task, status=status, run=1, branch="b")))
    if day is not None:
        event = "done" if status == "closed" else turn.CHECKED
        world.queue().log(task.id, f"2026-10-0{day}T10:00:00Z", "warden", event, "x", to=status)
    _file(world.places.evidence / task.id / ("c" * 12) / "qa-ux" / OLD / "shot.png", 1)
    return task.id


@pytest.mark.parametrize(
    ("keep", "gone"),
    [(1, ["t1", "t2", "t3", "c1", "c2"]), (2, ["t1", "t2", "c1"]), (4, [])],
    ids=["newest-bundle", "newest-two", "fewer-bundles-than-kept"],
)
def test_qa_past_the_bundles_kept_goes_but_never_that_of_a_task_not_yet_checked_or_of_a_live_session(
    world: World, home: Path, keep: int, gone: list[str]
) -> None:
    world.with_settings(qa__keep_bundles=keep)
    checks = {f"c{n}": _check(world, day) for n, day in enumerate((1, 3, 5, 7), 1)}  # c4 cleared no merge
    tasks = {
        "t1": _task(world, "checked", 1),
        "t2": _task(world, "closed", 2),
        "t3": _task(world, "promoted", 3),
        "t4": _task(world, "checked", 5),
        "t5": _task(world, "merged"),
        "t6": _task(world, "checked", 1),
    }
    world.session("s-live", "implementer", "impl-t6", task=tasks["t6"])
    runs = home / ".factory-qa"
    for run in (f"s-done/{OLD}", f"s-done/{NEW}", f"s-gone/{OLD}", f"s-live/{OLD}"):
        (runs / run).mkdir(parents=True)
    ids = {**checks, **tasks}

    removed = retention.remove(world.ctx(), runs)

    went = sorted(ids[name] for name in gone)
    assert removed == (len(went), 2 if went else 0, [])
    assert sorted(retention.cleaned(world.places)) == went
    assert sorted(p.name for p in world.places.evidence.iterdir() if p.is_dir()) == sorted(set(ids.values()) - {*went})
    rm = [["rm", "-rf", "--", str(runs / "s-done" / OLD)], ["rm", "-rf", "--", str(runs / "s-gone")]]
    assert world.launcher.commands == (rm if went else [])


@pytest.mark.parametrize(
    ("status", "remove", "live", "kept"),
    [
        ("checked", 0, False, False),
        ("closed", 0, False, False),
        ("checked", 128, False, True),  # still dirty, or locked by a process that outlived its session
        ("merged", 0, False, True),
        ("checked", 0, True, True),
    ],
    ids=["checked", "closed", "still-dirty", "not-yet-checked", "a-live-session-works-in-it"],
)
def test_a_held_worktree_goes_once_its_task_settled_and_git_calls_it_clean(
    world: World, tmp_path: Path, status: str, remove: int, live: bool, kept: bool
) -> None:
    task, worktree = _task(world, status), tmp_path / "wt"
    worktree.mkdir()
    world.session("s-old", "implementer", "impl-x", task=task, worktree=str(worktree))
    sessions = registry.read(world.places)
    sessions["s-old"] = dataclasses.replace(sessions["s-old"], state=RETIRED)
    registry.write(world.places, sessions)
    dirty = (128, "fatal: '/wt' contains modified or untracked files")
    world.launcher.git = lambda argv: (0, "impl-x\n") if argv[1] == "rev-parse" else dirty
    reap.reap(world.ctx(), "s-old")
    if live:
        world.session("s-next", "implementer", "impl-x", task=task, worktree=str(worktree))
    world.launcher.commands.clear()
    world.launcher.git = lambda argv: (remove, "") if argv[1] == "worktree" else (0, "")

    assert reap.retry_held(world.ctx()) == (0 if kept else 1)

    assert (reap.held_for(world.places, task) is not None) == kept
    went = [["git", "worktree", "remove", str(worktree)], ["git", "branch", "-d", "impl-x"]]
    assert world.launcher.commands == ([] if status == "merged" or live else went[:1] if kept else went)


@pytest.mark.parametrize(
    ("sizes", "ages", "incident"),
    [
        ([GIB], [["168", "48"]], False),
        ([61 * GIB, GIB], [["168", "48"], ["0", "0"]], False),
        ([61 * GIB, 61 * GIB], [["168", "48"], ["0", "0"]], True),
    ],
    ids=["under-budget", "under-budget-once-all-unused-went", "over-budget"],
)
def test_docker_data_past_its_ages_goes_then_all_that_is_unused_while_over_budget(
    world: World, sizes: list[int], ages: list[list[str]], incident: bool
) -> None:
    world.with_settings(gc__docker_budget_gib=60)
    left = iter(sizes)
    world.launcher.stack = lambda n, verb: StackResult(0, f"{next(left)}\n" if verb[0] == leases.DISK else "", "")

    assert gc.prune_docker(world.ctx()) == (sizes[-1], [])

    assert [verb for _, verb in world.launcher.stacks if verb[0] == leases.PRUNE] == [[leases.PRUNE, *a] for a in ages]
    over = [e for e in reflections.entries(world.places) if "over its 60 GiB budget" in e["text"]]
    assert bool(over) == incident


def _old_run_unremovable(world: World, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world.with_settings(qa__keep_bundles=1)
    _check(world, 5)
    _task(world, "checked", 5)
    (home / ".factory-qa" / "s-done" / OLD).mkdir(parents=True)
    world.launcher.git = lambda argv: (1, "rm: Permission denied")


def _prune_refused(world: World, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world.launcher.stack = lambda n, verb: StackResult(1 if verb[0] == leases.PRUNE else 0, "1024\n", "")


def _crash(world: World, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(ctx: Context, runs: Path) -> None:
        raise OSError("disk I/O error")

    monkeypatch.setattr(retention, "remove", broken)


@pytest.mark.parametrize(
    ("breaks", "failed", "logged"),
    [
        (
            _old_run_unremovable,
            "the runs in s-done could not be removed (exit 1)",
            "warden: the clean-up could not remove the runs in s-done (exit 1)",
        ),
        (_prune_refused, "the Docker prune failed (exit 1)", "warden: the Docker prune failed (exit 1)"),
        (_crash, "it stopped (OSError)", "warden: the clean-up failed (OSError)"),
    ],
    ids=["a-run-stays", "docker-refused", "it-stopped"],
)
def test_a_clean_up_that_fails_is_logged_and_turns_the_disk_vital_amber(
    world: World,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    breaks: Callable[[World, Path, pytest.MonkeyPatch], None],
    failed: str,
    logged: str,
) -> None:
    breaks(world, home, monkeypatch)
    cleanup = gc.Cleanup()

    with caplog.at_level(logging.WARNING, logger="factory.warden"):
        cleanup.run_pass(world.ctx())
        assert cleanup._thread is not None
        cleanup._thread.join()

    assert logged in caplog.text
    problem = gc.view(world.ctx())["problem"]
    assert problem == f"the last clean-up failed: {failed}"
    plenty = os.statvfs_result((4096, 4096, 100 * GIB // 4096, 0, 90 * GIB // 4096, 0, 0, 0, 0, 255))
    monkeypatch.setattr(vitals.os, "statvfs", lambda path: plenty)
    assert vitals.disk([world.places.state], world.settings, problem).level == vitals.AMBER


def test_what_fills_the_disk_is_measured_by_kind_and_a_folder_not_made_yet_holds_nothing(
    world: World, home: Path
) -> None:
    _file(world.repo / ".claude" / "worktrees" / "impl-t-0001" / "node_modules" / "big.js", 2 * MIB)
    _file(world.places.evidence / "t-0001" / "shot.png", MIB // 4)
    _file(home / ".factory-qa" / "s-1" / "run" / "frame.jpeg", MIB // 4)

    use = gc.measure(world.ctx(), 5 * GIB)

    assert use["docker"] == 5 * GIB and use["transcripts"] == 0
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
    last = {"started": "2026-10-09T06:00:00Z", "ended": "2026-10-09T06:01:00Z", "failed": [], "use": USE,
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
