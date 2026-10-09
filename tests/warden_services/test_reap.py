from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from warden_fakes import FakeLauncher

from runtime.shared.models import BENCH_PENDING, DEPRIORITIZED, RETIRED, REVIVING, WAITING, WORKING, Session
from runtime.shared.protocol import Refused
from runtime.warden import reap, registry
from runtime.warden.context import Context


def session(sid: str, state: str, worktree: str, **fields: Any) -> Session:
    return Session(id=sid, name=f"impl-{sid}", role="implementer", family="impl", model="m", effort="high",
                   task="t-0007", parent="", worktree=worktree, slot=2, state=state, started="2026-10-09T10:00:00Z",
                   started_by="orchestrator", **fields)  # fmt: skip


def lease(ctx: Context, holder: str) -> Path:
    path = ctx.places.slot(2) / "lease"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"checkout=/w\nholder={holder}\nsince=2026-10-09T10:00:00Z\nnote=\n")
    return path


@pytest.mark.parametrize("state", [WORKING, WAITING, BENCH_PENDING, DEPRIORITIZED, REVIVING])
def test_only_a_retired_session_is_reaped(ctx: Context, launcher: FakeLauncher, tmp_path: Path, state: str) -> None:
    (tmp_path / "wt").mkdir()
    registry.write(ctx.places, {"s1": session("s1", state, str(tmp_path / "wt"))})
    held = lease(ctx, "s1")

    with pytest.raises(Refused) as refused:
        reap.reap(ctx, "s1")
    reap.Reaper().run_pass(ctx)

    assert refused.value.rule == "reap"
    assert launcher.calls == []
    assert held.exists() and (tmp_path / "wt").is_dir()


def test_a_retired_session_never_touches_what_a_live_one_holds(
    ctx: Context, launcher: FakeLauncher, tmp_path: Path
) -> None:
    (tmp_path / "wt").mkdir()
    sessions = {
        "old": session("old", RETIRED, str(tmp_path / "wt")),
        "new": session("new", DEPRIORITIZED, str(tmp_path / "wt")),  # took the task's worktree and the slot over
    }
    registry.write(ctx.places, sessions)
    lease(ctx, "new")

    outcome = reap.reap(ctx, "old")

    assert outcome == {
        "slot": "left alone: another session holds the slot",
        "worktree": "left alone: a live session works in it",
    }
    assert launcher.calls == []


def test_a_dirty_worktree_is_held_for_the_tasks_next_session(
    ctx: Context, launcher: FakeLauncher, tmp_path: Path
) -> None:
    (tmp_path / "wt").mkdir()
    registry.write(ctx.places, {"s1": session("s1", RETIRED, str(tmp_path / "wt"))})
    held_lease = lease(ctx, "s1")
    launcher.commands[("git", "rev-parse")] = (0, "fix-sign-in\n")
    launcher.commands[("git", "worktree", "remove")] = (128, "fatal: '/w' contains modified or untracked files")

    outcome = reap.reap(ctx, "s1")

    assert outcome["slot"] == "emptied" and not held_lease.exists()
    assert [c[1][1] for c in launcher.calls if c[0] == "stack"] == [["empty"], [reap.SWEEP_SLOT]]
    assert outcome["worktree"].startswith("held: fatal:")
    held = reap.held_for(ctx.places, "t-0007")
    assert held is not None and (held["branch"], held["session"]) == ("fix-sign-in", "s1")
    assert not any(c[1][0][:2] == ["git", "branch"] for c in launcher.calls if c[0] == "command")
