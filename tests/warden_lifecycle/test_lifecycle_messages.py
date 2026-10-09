from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from lifecycle_fakes import World

from runtime.shared.models import BENCH_PENDING, DEPRIORITIZED, RETIRED, WORKING
from runtime.shared.protocol import Refused
from runtime.warden import messages


def test_a_running_session_gets_the_owners_message_as_its_own_turn(world: World) -> None:
    world.seat("5e550001")
    world.ctx(role="dashboard", kind="dashboard")
    messages.send(world.owner(), {"session": "5e550001", "text": "  Skip the dark-mode pass.  "}, None)
    assert world.launcher.messages == [("5e550001", messages.PREFIX + "Skip the dark-mode pass.")]


@pytest.mark.parametrize(
    ("fields", "rule"),
    [({"parent": "0c0c0c0c", "agent_id": "a1"}, "subagent"), ({"state": RETIRED, "retired": "finished"}, "retired"),
     ({"state": BENCH_PENDING}, "settling")],
    ids=["subagent", "retired", "settling"],
)  # fmt: skip
def test_a_message_goes_only_to_a_session_that_can_take_a_turn(world: World, fields: dict[str, Any], rule: str) -> None:
    world.seat("0c0c0c0c", role="orchestrator", family="orch")
    world.seat("5e550001", **fields)
    with pytest.raises(Refused) as refused:
        messages.send(world.owner(), {"session": "5e550001", "text": "hello"}, None)
    assert refused.value.rule == rule and world.launcher.messages == []


def test_a_benched_session_comes_back_with_the_message(world: World) -> None:
    world.queue.add("t-0001", status="deprioritized")
    world.seat("5e550001", task="t-0001", state=DEPRIORITIZED)
    messages.send(world.owner(), {"session": "5e550001", "text": "Finish the menu first."}, None)
    resume = world.launcher.commands[-1]
    assert resume[:3] == ["claude", "--resume", "5e550001"] and resume[-1].endswith(
        messages.PREFIX + "Finish the menu first."
    )
    assert world.session("5e550001").state == WORKING


def test_the_thread_pairs_each_owner_message_with_what_the_agent_said_after_it(world: World, tmp_path: Path) -> None:
    s = world.seat("5e550001", worktree="/srv/shop")

    def say(kind: str, content: object) -> dict[str, Any]:
        return {"type": kind, "timestamp": "2026-10-09T14:02:00Z", "message": {"content": content}}

    records = [
        say("user", "Start your loop."),
        say("assistant", [{"type": "text", "text": "Starting."}]),
        say("user", messages.PREFIX + "Skip dark mode."),
        say("assistant", [{"type": "thinking", "thinking": "..."}, {"type": "text", "text": "Understood."},
                          {"type": "tool_use", "name": "Bash"}]),
        say("user", [{"type": "tool_result", "content": "ok"}]),
        say("assistant", [{"type": "text", "text": "Dropped the slice."}]),
        say("user", "The warden brought you back."),
        say("assistant", [{"type": "text", "text": "Back at it."}]),
        say("user", messages.PREFIX + "Status?"),
    ]  # fmt: skip
    folder = tmp_path / "agent" / ".claude" / "projects" / "-srv-shop"
    folder.mkdir(parents=True)
    (folder / "5e550001.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")
    thread = messages.thread(world.owner(), s)
    assert [(e["text"], [r["text"] for r in e["replies"]]) for e in thread] == [
        ("Skip dark mode.", ["Understood.", "Dropped the slice."]),
        ("Status?", []),
    ]
