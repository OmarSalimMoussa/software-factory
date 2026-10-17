from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from warden_fakes import FakeLauncher

from runtime.guard.facts import read_desktops
from runtime.shared import store
from runtime.shared.models import BENCH_PENDING, DEPRIORITIZED, RETIRED, REVIVING, WAITING, WORKING, Session
from runtime.shared.protocol import Refused
from runtime.slots import leases
from runtime.warden import public
from runtime.warden.context import Context, StackResult
from runtime.warden.sessions import reap, registry


def session(sid: str, state: str, worktree: str, **fields: Any) -> Session:
    return Session(id=sid, name=f"impl-{sid}", role="implementer", family="impl", model="m", effort="high",
                   task="t-0007", parent="", worktree=worktree, state=state, started="2026-10-09T10:00:00Z",
                   started_by="orchestrator", **fields)  # fmt: skip


def lease(ctx: Context, holder: str) -> Path:
    path = ctx.places.slot(2) / "lease"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"holder={holder}\nsince=2026-10-09T10:00:00Z\nnote=\n")
    return path


@pytest.mark.parametrize("state", [WORKING, WAITING, BENCH_PENDING, DEPRIORITIZED, REVIVING])
def test_only_a_retired_session_is_reaped(ctx: Context, launcher: FakeLauncher, tmp_path: Path, state: str) -> None:
    (tmp_path / "wt").mkdir()
    registry.write(ctx.places, {"s1": session("s1", state, str(tmp_path / "wt"))})
    held = lease(ctx, "impl-s1")

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
    lease(ctx, "impl-new")

    outcome = reap.reap(ctx, "old")

    assert outcome == {"worktree": "left alone: a live session works in it"}
    assert launcher.calls == []


def test_a_dirty_worktree_is_held_for_the_tasks_next_session(
    ctx: Context, launcher: FakeLauncher, tmp_path: Path
) -> None:
    (tmp_path / "wt").mkdir()
    registry.write(ctx.places, {"s1": session("s1", RETIRED, str(tmp_path / "wt"))})
    held_lease = lease(ctx, "impl-s1")
    launcher.commands[("git", "rev-parse")] = (0, "fix-sign-in\n")
    launcher.commands[("git", "worktree", "remove")] = (128, "fatal: '/w' contains modified or untracked files")

    outcome = reap.reap(ctx, "s1")

    assert outcome["slot"] == "emptied" and not held_lease.exists()
    assert [c[1][1] for c in launcher.calls if c[0] == "stack"] == [["empty"], [leases.SWEEP]]
    assert outcome["worktree"].startswith("held: fatal:")
    held = reap.held_for(ctx.places, "t-0007")
    assert held is not None and (held["branch"], held["session"]) == ("fix-sign-in", "s1")
    assert not any(c[1][0][:2] == ["git", "branch"] for c in launcher.calls if c[0] == "command")


def test_a_reap_that_could_not_empty_the_slot_is_tried_again_after_its_wait(
    ctx: Context, launcher: FakeLauncher, tmp_path: Path
) -> None:
    registry.write(ctx.places, {"s1": session("s1", RETIRED, "")})
    held = lease(ctx, "impl-s1")
    launcher.stack["empty"] = StackResult(exit=1, tail="", log="stack.log")
    reaper = reap.Reaper()

    def pass_() -> None:
        reaper.run_pass(ctx)
        assert reaper._thread is not None
        reaper._thread.join()

    pass_()
    assert reap.reaped(ctx.places)["s1"]["slot"].startswith("failed") and held.exists()
    pass_()  # within the wait: not tried again
    assert sum(1 for c in launcher.calls if c[0] == "stack") == 1
    ctx.clock.t += ctx.settings().get_int("watch.reap_retry_minutes") * 60  # type: ignore[attr-defined]  # the fake clock
    del launcher.stack["empty"]
    pass_()
    assert reap.reaped(ctx.places)["s1"]["slot"] == "emptied" and not held.exists()


@pytest.mark.parametrize("exit_code", [0, 1])
def test_a_desktop_its_tester_handed_back_goes_down_and_its_lease_goes_even_when_it_would_not(
    ctx: Context, launcher: FakeLauncher, caplog: pytest.LogCaptureFixture, exit_code: int
) -> None:
    registry.write(ctx.places, {"s1": session("s1", WORKING, "/wt"), "a1": session("a1", RETIRED, "/wt"),
                                "a2": session("a2", WORKING, "/wt")})  # fmt: skip
    store.write_json(public.desktops_path(ctx.places), {"2": "a1", "3": "a2"})
    launcher.stack["desktop"] = StackResult(exit=exit_code, tail="", log="stack.log")
    reaper = reap.Reaper()

    reaper.run_pass(ctx)
    assert reaper._thread is not None
    reaper._thread.join()

    assert [c[1] for c in launcher.calls if c[0] == "stack"] == [(2, ["desktop", "down"], "a1")]
    assert read_desktops(ctx.places) == {"3": "a2"}
    assert ("did not go down" in caplog.text) is bool(exit_code)
