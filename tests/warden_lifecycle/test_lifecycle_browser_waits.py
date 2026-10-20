from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest
from lifecycle_fakes import World

from runtime.guard import decide
from runtime.guard.facts import Refusal
from runtime.shared import lanes, store
from runtime.shared.models import RETIRED
from runtime.warden import public
from runtime.warden.sessions import browser_waits, subagents
from runtime.warden.sessions.launch import LaunchError

HELD = {"rule": "browsers-at-once", "tool": "Agent", "detail": "1 browser testers run on this machine, its cap of 1"}
RECENT = "2026-10-09T11:59:00Z"
ORCH, IMPL = "0rch0001", "1mp10001"
WANTS = {ORCH: ("orchestrator", "qa-regression"), IMPL: ("implementer", "qa-ux")}


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


@pytest.mark.parametrize(
    ("refusals", "told"),
    [
        ([["1mp10001", "1mp10002", "0rch0001"]], ["0rch0001"]),
        ([["1mp10001", "1mp10002"], ["0rch0001", "1mp10001"]], ["1mp10001", "0rch0001"]),
    ],
    ids=["joins", "ahead-of-a-told-session-refused-again"],
)
def test_the_orchestrator_refused_a_browser_place_goes_to_the_head_of_the_line(
    world: World, refusals: list[list[str]], told: list[str]
) -> None:
    _line(world, 2, 1)
    world.seat("0rch0001", role="orchestrator")
    world.run("0rch0001")
    for sids in refusals:
        _refuse(world, *sids)
        browser_waits.run_pass(world.ctx())

    assert [sid for sid, _ in world.launcher.messages] == told


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


def test_a_place_told_of_is_taken_while_the_orchestrator_waits_and_the_orchestrator_is_told_of_the_next(
    world: World, tmp_path: Path
) -> None:
    """The live loop: refused for the place kept for the implementer, the orchestrator waited; the guard then refused
    the implementer for that wait, and the warden told it of the place on every pass, so neither tester started."""
    world.settings = world.settings.with_changes({"caps.browsers_at_once": 2})
    public.write_settings(world.places, world.settings)
    public.write_switches(world.places, ["qa-ready"])
    public.write_vitals(world.places, "green", "", 30.0, RECENT)
    for sid, (role, kind) in WANTS.items():
        world.seat(sid, role=role)
        world.run(sid)
        world.seat(f"{kind}-1", role=kind, parent=sid, agent_id=f"{kind}-1", started=RECENT)
    refused, told = [_start(world, tmp_path, IMPL)], []
    subagents.ended(world.ctx(session=ORCH), {"agent_id": "qa-regression-1"}, None)
    told += _pass(world, tmp_path, answer=False)  # its own subagents filled its cap, so it starts nothing yet
    refused += [_start(world, tmp_path, ORCH), _start(world, tmp_path, IMPL)]
    for _ in range(4):
        told += _pass(world, tmp_path)
    subagents.ended(world.ctx(session=IMPL), {"agent_id": "qa-ux-1"}, None)
    told += _pass(world, tmp_path)

    assert [why.split(", its cap")[0] for why in refused if why] == [
        "2 browser testers run on this machine",
        "1 browser testers run on this machine, and 1 kept for a session told one is free",
    ]
    assert told == [IMPL, ORCH]


def _pass(world: World, tmp_path: Path, answer: bool = True) -> list[str]:
    """A warden pass, 30 s on, and the start each session told of a place tries then."""
    world.later(30)
    seen = len(world.launcher.messages)
    browser_waits.run_pass(world.ctx())
    told = [sid for sid, _ in world.launcher.messages[seen:]]
    for sid in told if answer else []:
        _start(world, tmp_path, sid)
    return told


def _start(world: World, tmp_path: Path, sid: str) -> str:
    role, kind = WANTS[sid]
    transcript = tmp_path / f"{sid}.jsonl"
    transcript.touch()
    brief = {"subagent_type": kind, "prompt": "Scenarios and checks: 1\nWalk the sign-in."}
    payload = {"session_id": sid, "tool_name": "Agent", "tool_input": brief, "transcript_path": str(transcript)}
    call = decide.resolve(payload, world.places, datetime.fromtimestamp(world.clock.now(), UTC), tmp_path)
    assert call is not None
    decision = decide.decide_tool(call)
    if isinstance(decision, Refusal):
        assert decision.rule == lanes.PLACES, decision.reason
        browser_waits.refused(world.ctx(), {"rule": decision.rule, "session": sid, "detail": decision.reason})
        return decision.reason
    started = f"{kind}-{world.clock.now():.0f}"
    subagents.started(world.ctx(role=role, session=sid), {"agent_id": started, "kind": kind}, None)
    return ""


@pytest.mark.parametrize("ends", ["taken-down", "holder-retires"])
def test_no_session_is_told_of_a_place_while_a_tester_holds_a_desktop(world: World, ends: str) -> None:
    _line(world, 2, 1)
    store.write_json(public.desktops_path(world.places), {"1": "a0"})
    _refuse(world, "1mp10001")
    browser_waits.run_pass(world.ctx())
    assert world.launcher.messages == []

    if ends == "taken-down":
        store.write_json(public.desktops_path(world.places), {})
    else:
        world.seat("a0", role="qa-ux", parent="1mp10004", agent_id="a0", started=RECENT, state=RETIRED,
                   retired="finished")  # fmt: skip
    browser_waits.run_pass(world.ctx())

    assert world.launcher.messages == [("1mp10001", browser_waits.FREE)]


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
