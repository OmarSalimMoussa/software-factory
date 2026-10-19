from __future__ import annotations

import logging

import pytest
from lifecycle_fakes import World

from runtime.shared.models import RETIRED
from runtime.warden.sessions import browser_waits, subagents
from runtime.warden.sessions.launch import LaunchError

HELD = {"rule": "browsers-at-once", "tool": "Agent", "detail": "1 browser testers run on this machine, its cap of 1"}
RECENT = "2026-10-09T11:59:00Z"


def _line(world: World, cap: int, testers: int) -> None:
    """Four running implementers, and ``testers`` browser testers of the fourth holding places out of ``cap``."""
    world.settings = world.settings.with_changes({"caps.browsers_at_once": cap})
    for sid in ("1mp10001", "1mp10002", "1mp10003", "1mp10004"):
        world.seat(sid)
        world.run(sid)
    for n in range(testers):
        world.seat(f"a{n}", role="qa-ux", parent="1mp10004", agent_id=f"a{n}", started=RECENT)


def _refuse(world: World, *sids: str) -> None:
    for sid in sids:
        browser_waits.refused(world.ctx(), {**HELD, "session": sid})


@pytest.mark.parametrize(
    ("cap", "level", "told"),
    [(1, "green", []), (2, "green", ["1mp10002"]), (3, "green", ["1mp10002", "1mp10001"]), (3, "red", [])],
    ids=["every-place-held", "one-place-free", "two-places-free", "red-vitals"],
)
def test_a_session_refused_a_browser_place_is_told_once_one_frees_the_longest_waiting_first(
    world: World, cap: int, level: str, told: list[str]
) -> None:
    _line(world, cap, 1)
    world.vitals(level)
    _refuse(world, "1mp10002", "1mp10001", "1mp10002")

    browser_waits.run_pass(world.ctx())
    browser_waits.run_pass(world.ctx())

    assert world.launcher.messages == [(sid, browser_waits.FREE) for sid in told]


def test_the_orchestrator_refused_a_browser_place_goes_to_the_head_of_the_line(world: World) -> None:
    _line(world, 2, 1)
    world.seat("0rch0001", role="orchestrator")
    world.run("0rch0001")
    _refuse(world, "1mp10001", "1mp10002", "0rch0001")

    browser_waits.run_pass(world.ctx())

    assert [sid for sid, _ in world.launcher.messages] == ["0rch0001"]


def test_a_told_session_refused_again_goes_first_and_a_place_kept_past_its_turn_goes_to_the_next(world: World) -> None:
    _line(world, 2, 1)
    _refuse(world, "1mp10001", "1mp10002")
    browser_waits.run_pass(world.ctx())
    _refuse(world, "1mp10001")  # another session took the place it was told of
    browser_waits.run_pass(world.ctx())
    world.later(world.settings.get_int("qa.place_turn_seconds"))
    browser_waits.run_pass(world.ctx())

    assert [sid for sid, _ in world.launcher.messages] == ["1mp10001", "1mp10001", "1mp10002"]


def test_a_session_leaves_the_line_once_it_starts_a_browser_tester_or_retires(world: World) -> None:
    _line(world, 2, 1)
    _refuse(world, "1mp10001", "1mp10002", "1mp10003")
    subagents.started(world.ctx(role="implementer", session="1mp10001"), {"agent_id": "b1", "kind": "qa-visual"}, None)
    world.seat("1mp10002", state=RETIRED, retired="finished")
    world.seat("a0", role="qa-ux", parent="1mp10004", agent_id="a0", started=RECENT, state=RETIRED, retired="finished")

    browser_waits.run_pass(world.ctx())

    assert world.launcher.messages == [("1mp10003", browser_waits.FREE)]


def test_a_place_the_warden_could_not_tell_of_is_logged(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _line(world, 2, 1)
    _refuse(world, "1mp10001")
    monkeypatch.setattr(world.launcher, "message_session", _unreachable)

    with caplog.at_level(logging.WARNING):
        browser_waits.run_pass(world.ctx())

    assert "1mp10001" in caplog.text and "LaunchError" in caplog.text


def _unreachable(session_id: str, text: str) -> None:
    raise LaunchError("gone")
