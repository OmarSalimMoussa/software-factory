from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest
from lifecycle_fakes import World

from runtime.shared import store
from runtime.shared.models import BENCH_PENDING, DEPRIORITIZED, RETIRED, WORKING
from runtime.shared.protocol import Refused
from runtime.warden.owner import messages, ops, yolo
from runtime.warden.sessions import lifecycle, revive
from runtime.warden.sessions.launch import LaunchError


@pytest.mark.parametrize("reaches", [True, False], ids=["it-arrives", "the-session-cannot-be-reached"])
def test_a_running_session_gets_the_owners_message_as_its_own_turn_while_the_owner_waits_for_nothing(
    world: World, monkeypatch: pytest.MonkeyPatch, reaches: bool
) -> None:
    world.seat("5e550001")
    world.ctx(role="dashboard", kind="dashboard")
    if not reaches:
        monkeypatch.setattr(world.launcher, "message_session", _unreachable)

    answer = messages.send(world.owner(), {"session": "5e550001", "text": "  Skip the dark-mode pass.  "}, None)
    ops.join(answer["op"], 10)

    op = ops.get(world.owner(), {"id": answer["op"]}, None)["op"]
    if reaches:
        assert world.launcher.messages == [("5e550001", messages.PREFIX + "Skip the dark-mode pass.")]
        assert op["state"] == ops.DONE
    else:
        assert (op["state"], op["steps"][0]["detail"]) == (ops.FAILED, "the message did not reach the session: gone")


def _unreachable(session_id: str, text: str) -> None:
    raise LaunchError("gone")


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


@pytest.mark.parametrize(("role", "takes"), [("orchestrator", True), ("implementer", False)])
def test_under_yolo_mode_only_the_orchestrator_takes_the_owners_message(world: World, role: str, takes: bool) -> None:
    yolo.owner_switch(world.owner(), {"on": True}, None)
    world.seat("5e550001", role=role, family="orch" if role == "orchestrator" else "impl")

    if takes:
        ops.join(messages.send(world.owner(), {"session": "5e550001", "text": "Carry on."}, None)["op"])
    else:
        with pytest.raises(Refused) as refused:
            messages.send(world.owner(), {"session": "5e550001", "text": "Carry on."}, None)
        assert refused.value.rule == "yolo"
    assert bool(world.launcher.messages) is takes


def test_a_benched_session_comes_back_with_the_message(world: World) -> None:
    world.queue.add("t-0001", status="deprioritized")
    world.seat("5e550001", task="t-0001", state=DEPRIORITIZED)
    ops.join(messages.send(world.owner(), {"session": "5e550001", "text": "Finish the menu first."}, None)["op"])
    [(sid, said)] = world.launcher.messages
    assert world.launcher.resumed == [sid] == ["5e550001"] and said.endswith(messages.PREFIX + "Finish the menu first.")
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


@pytest.mark.parametrize("action", ["message", "message-to-the-bench", "resume"])
def test_the_owners_action_answers_before_the_session_hears_it(
    world: World, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    # Handing a session its word takes longer than the dashboard waits for an answer: a cited bug said "did not go
    # through" for a message that did.
    let_go, heard = threading.Event(), world.launcher.message_session

    def slow(session_id: str, text: str) -> None:
        let_go.wait(10)
        heard(session_id, text)

    monkeypatch.setattr(world.launcher, "message_session", slow)
    benched = DEPRIORITIZED if action != "message" else WORKING
    world.seat("0c0c0c0c", role="orchestrator", family="orch", state=benched)
    if action == "message":
        world.run("0c0c0c0c")
    if action == "resume":
        store.write_json(lifecycle.paused_path(world.ctx()), {"at": "", "sessions": ["0c0c0c0c"], "op": "op-paused"})
        answer = revive.owner_resume(world.owner(), {}, None)
    else:
        answer = messages.send(world.owner(), {"session": "0c0c0c0c", "text": "Carry on."}, None)
    assert answer["op"] and world.launcher.messages == []
    let_go.set()
    ops.join(answer["op"], 10)
    assert [sid for sid, _ in world.launcher.messages] == ["0c0c0c0c"]
    assert ops.get(world.owner(), {"id": answer["op"]}, None)["op"]["state"] == ops.DONE
