"""Who is calling decides what is left: the owner's own session, a session the warden never started, a retired one,
a stopped subagent, a session being benched; and what a session reads at start and needs before it goes idle."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from guard_kit import NOW, SID, TESTER_ID, Kit

from runtime.guard import decide, transcript
from runtime.guard.facts import Refusal
from runtime.shared.models import PENDING


@pytest.mark.parametrize(
    ("tool", "tool_input", "refused"),
    [
        ("Agent", {"subagent_type": "Explore", "prompt": "p"}, True),
        ("Edit", {"file_path": "/w/app.py"}, True),
        ("Bash", {"command": "pytest"}, True),
        ("Bash", {"command": "git log -3"}, False),
        ("Bash", {"command": "git -C /w log --oneline -1"}, False),
        ("Bash", {"command": "git -C /w push origin HEAD:main"}, True),
        ("Read", {"file_path": "/w/app.py"}, False),
        ("SendMessage", {"to": "main", "message": "stuck"}, False),
    ],
)
def test_a_session_the_warden_never_started_may_only_read_and_report(
    tmp_path: Path, tool: str, tool_input: dict[str, Any], refused: bool
) -> None:
    assert (Kit(tmp_path).rule(tool, tool_input) == "unpermitted") is refused


def test_the_owners_registered_session_is_left_alone(tmp_path: Path) -> None:
    kit = Kit(tmp_path)
    kit.session("owner")
    payload = kit.payload("Bash", {"command": "claude --bg x"})
    assert decide.resolve(payload, kit.places, NOW, kit.home) is None


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [("Read", {"file_path": "/w/app.py"}), ("Bash", {"command": "ls"}), ("SubagentHandback", {"message": "x"})],
)
def test_every_call_of_a_retired_session_is_refused(tmp_path: Path, tool: str, tool_input: dict[str, Any]) -> None:
    kit = Kit(tmp_path)
    kit.session("implementer", state="retired")
    assert kit.rule(tool, tool_input) == "retired"


def test_a_stopped_subagent_may_only_hand_back_and_its_siblings_run_on(tmp_path: Path) -> None:
    kit = Kit(tmp_path)
    kit.session("implementer")
    kit.session("implementer", sid="sub-1", state="retired", agent_id=TESTER_ID, parent=SID)
    kit.subagent(TESTER_ID, [])
    stopped = {"agent_id": TESTER_ID, "agent_type": "Explore"}
    assert kit.rule("Read", {"file_path": "/w/app.py"}, **stopped) == "stopped-subagent"
    assert kit.rule("SubagentHandback", {"message": "2 of 3"}, **stopped) is None
    sibling = {"agent_id": "a9999999999999", "agent_type": "Explore"}
    assert kit.rule("Read", {"file_path": "/w/app.py"}, **sibling) is None


@pytest.mark.parametrize(
    ("tool", "tool_input", "refused"),
    [
        ("Bash", {"command": "git add -A && git commit -m 'wip: paused'"}, False),
        ("Bash", {"command": "git status"}, False),
        ("Bash", {"command": "factory doing 'paused'"}, False),
        ("Read", {"file_path": "/w/app.py"}, False),
        ("Bash", {"command": "uv run pytest"}, True),
        ("Bash", {"command": "git commit -m x && make build"}, True),
        ("Edit", {"file_path": "{worktree}/app.py", "old_string": "a", "new_string": "b"}, True),
        ("Agent", {"subagent_type": "qa-backend", "prompt": "Scenarios and checks: 1"}, True),
    ],
)
def test_a_session_being_benched_may_only_commit_and_ask_the_factory(
    tmp_path: Path, tool: str, tool_input: dict[str, Any], refused: bool
) -> None:
    kit = Kit(tmp_path)
    kit.session("implementer", state="bench-pending")
    asked = {k: v.format(worktree=kit.worktree) if isinstance(v, str) else v for k, v in tool_input.items()}
    decision = kit.decide(tool, asked)
    assert (isinstance(decision, Refusal) and decision.rule == "bench-pending") is refused
    if refused:
        assert isinstance(decision, Refusal) and "commit your work in progress" in decision.reason


@pytest.mark.parametrize(
    ("agent_type", "says"),
    [("qa-ux", "run `factory qa result`, then hand back"), ("Explore", "hand back your report now")],
)
def test_a_subagent_of_a_session_being_benched_is_told_to_report_and_hand_back(
    tmp_path: Path, agent_type: str, says: str
) -> None:
    kit = Kit(tmp_path)
    kit.session("implementer", state="bench-pending")
    decision = kit.decide("Bash", {"command": "uv run pytest"}, agent_id="a1b2c3", agent_type=agent_type)
    assert isinstance(decision, Refusal) and decision.rule == "bench-pending"
    assert "being benched" in decision.reason and decision.reason.endswith(says)
    assert "commit" not in decision.reason


@pytest.mark.parametrize(
    ("file", "refused"), [(".factory/f-1.md", False), ("app/notes.py", True)], ids=["its-report", "the-code"]
)
def test_a_tester_of_a_session_being_benched_still_writes_its_report(tmp_path: Path, file: str, refused: bool) -> None:
    kit = Kit(tmp_path)
    kit.session("implementer", state="bench-pending")
    asked = {"file_path": str(kit.worktree / file), "content": "Save fails."}
    assert (kit.rule("Write", asked, agent_id="a1b2c3", agent_type="qa-ux") == "bench-pending") is refused


def test_each_decision_reads_the_transcript_as_it_is_though_its_size_and_time_did_not_change(tmp_path: Path) -> None:
    kit = Kit(tmp_path)
    kit.session("implementer")
    path = kit.transcript(['{"n": 1}'])
    os.utime(path, ns=(1, 1))
    assert [e["n"] for e in transcript.entries(path)] == [1]
    kit.transcript(['{"n": 2}'])
    os.utime(path, ns=(1, 1))
    kit.decide("Read", {"file_path": "/x"})
    assert [e["n"] for e in transcript.entries(path)] == [2]


@pytest.mark.parametrize("rule", ["raw-starts", "retired"])
def test_a_switched_off_rule_lets_the_call_through(tmp_path: Path, rule: str) -> None:
    kit = Kit(tmp_path)
    kit.session("implementer", state="retired" if rule == "retired" else "working")
    kit.switch_off(rule, "retired")
    assert kit.bash("claude --bg x" if rule == "raw-starts" else "ls") is None


def _start(kit: Kit) -> str:
    call = decide.resolve({"hook_event_name": "SessionStart", "session_id": SID}, kit.places, NOW, kit.home)
    assert call is not None
    return decide.session_context(call)


def test_a_session_starts_with_its_goal_and_the_note_on_reflecting(tmp_path: Path) -> None:
    kit = Kit(tmp_path)
    kit.session("implementer")
    context = _start(kit)
    assert str(kit.worktree / "GOAL.md") in context
    assert "SELF_REFLECT.md" in context


@pytest.mark.parametrize(("state", "says"), [("retired", "retired"), ("bench-pending", "commit your work")])
def test_a_retired_or_benched_session_is_told_so_at_start(tmp_path: Path, state: str, says: str) -> None:
    kit = Kit(tmp_path)
    kit.session("implementer", state=state)
    assert says in _start(kit)


def _shell(command: str) -> dict[str, str]:
    return {"id": "b1", "type": "shell", "status": "running", "command": command}


@pytest.mark.parametrize(
    ("tasks", "extra", "state", "refused"),
    [
        ([_shell("factory inbox --wait")], {}, "working", False),
        ([], {}, "working", True),
        (None, {}, "working", True),
        ([_shell("factory inbox")], {}, "working", True),
        ([_shell("factory inbox --wait-x")], {}, "working", True),
        ([{"id": "a1", "type": "subagent", "description": "factory inbox --wait"}], {}, "working", True),
        ([], {"stop_hook_active": True}, "working", False),
        ([], {"agent_id": "a1"}, "working", False),
        ([], {}, "bench-pending", False),
    ],
)
def test_the_orchestrator_cannot_go_idle_without_its_inbox_wait(
    tmp_path: Path, tasks: list[dict[str, str]] | None, extra: dict[str, object], state: str, refused: bool
) -> None:
    kit = Kit(tmp_path)
    kit.session("orchestrator", worktree=kit.repo, state=state)
    payload: dict[str, object] = {"hook_event_name": "Stop", "session_id": SID, "stop_hook_active": False, **extra}
    if tasks is not None:
        payload["background_tasks"] = tasks
    call = decide.resolve(payload, kit.places, NOW, kit.home)
    assert call is not None
    decision = decide.check_stop(call)
    assert (isinstance(decision, Refusal) and decision.rule == "inbox-wait") is refused


def _entry(role: str, content: object, **extra: object) -> str:
    return json.dumps({"type": role, "message": {"role": role, "content": content}, **extra})


def _send(to: str) -> str:
    return _entry("assistant", [{"type": "tool_use", "id": "m1", "name": "SendMessage", "input": {"to": to}}])


_PROMPT = _entry("user", "Build t-0001.")
_SENT = _entry("user", [{"type": "tool_result", "tool_use_id": "m1", "content": "sent"}], toolUseResult={"ok": True})
_OWNER = _entry("user", "Message from the owner, sent from the dashboard: is the fix right?", origin={"kind": "human"})
_REPLY = _entry("user", "Another Claude session sent a message: go on", isMeta=True, origin={"kind": "peer"})
_NOTICE = _entry("user", "<task-notification>qa-ux finished</task-notification>", origin={"kind": "task-notification"})
_SKILL = _entry("user", [{"type": "text", "text": "Base directory for this skill: …"}], isMeta=True)
_ORCH = {"kind": "peer", "name": "orchestrator", "from": "uds:/run/orch.sock"}
_ORCH_SAYS = _entry("user", "Another Claude session sent a message: you're next", isMeta=True, origin=_ORCH)
_NOTE = _entry("user", [{"type": "text", "text": "<system-reminder> [SYSTEM NOTIFICATION] a background-task event"}])
_TO_ORCH, _TO_TESTER, _TO_ADDRESS = _send("orchestrator"), _send("qa-ux"), _send("uds:/run/orch.sock")


@pytest.mark.parametrize(
    ("role", "lines", "extra", "state", "refused"),
    [
        ("implementer", [_PROMPT], {}, "working", True),
        ("implementer", [_PROMPT, _TO_ORCH, _SENT], {}, "working", False),
        ("implementer", [_PROMPT, _TO_ORCH, _SENT, _OWNER], {}, "working", True),
        ("implementer", [_PROMPT, _TO_TESTER, _SENT], {}, "working", True),
        ("implementer", [_PROMPT, _TO_ORCH, _SENT, _REPLY], {}, "working", True),
        ("implementer", [_PROMPT, _TO_ORCH, _SENT, _NOTICE], {}, "working", True),
        ("implementer", [_PROMPT, _TO_ORCH, _SENT, _SKILL], {}, "working", False),
        ("implementer", [_PROMPT, _ORCH_SAYS, _TO_ADDRESS, _SENT], {}, "working", False),
        ("implementer", [_PROMPT, _TO_ORCH, _SENT, _NOTE], {}, "working", True),
        ("implementer", [_PROMPT], {"background_tasks": [{"id": "a1", "type": "subagent"}]}, "working", False),
        ("implementer", [_PROMPT], {"background_tasks": [_shell("scripts/dev run")]}, "working", True),
        ("implementer", [_PROMPT], {"background_tasks": [{"type": "subagent", "status": "done"}]}, "working", True),
        ("implementer", [_PROMPT], {"stop_hook_active": True}, "working", True),
        ("implementer", [_PROMPT], {}, "waiting", False),
        ("implementer", [_PROMPT], {}, "bench-pending", False),
        ("implementer", [_PROMPT], {"agent_id": "a1"}, "working", False),
        ("designer", [_PROMPT], {}, "working", True),
    ],
    ids=["nothing", "told-the-orchestrator", "the-owner-spoke-after", "told-a-tester", "the-orchestrator-answered",
         "a-notice-came", "a-skill-loaded", "replied-to-its-address", "a-system-notice-came", "a-tester-runs",
         "only-a-command-runs", "a-tester-finished",
         "turned-back-again", "waits-on-a-card", "being-benched", "a-subagent", "designer"],
)  # fmt: skip
def test_an_implementer_or_designer_goes_idle_only_with_something_to_wake_it(
    tmp_path: Path, role: str, lines: list[str], extra: dict[str, object], state: str, refused: bool
) -> None:
    kit = Kit(tmp_path)
    kit.session(role, state=state)
    kit.transcript(lines)
    payload = {"hook_event_name": "Stop", "session_id": SID, "stop_hook_active": False, "cwd": str(kit.worktree),
               "transcript_path": str(kit.session_transcript), **extra}  # fmt: skip
    call = decide.resolve(payload, kit.places, NOW, kit.home)
    assert call is not None
    decision = decide.check_stop(call)
    assert (isinstance(decision, Refusal) and decision.rule == "no-waker") is refused


def test_a_session_the_warden_has_not_bound_yet_is_known_by_the_name_it_was_started_under(tmp_path: Path) -> None:
    kit = Kit(tmp_path)
    kit.session("implementer", sid=PENDING + "0f0f")
    # Claude Code's own record of the running session pairs the id the hook sees with the name the warden gave it.
    records = kit.home / ".claude" / "sessions"
    records.mkdir(parents=True)
    (records / "4242.json").write_text(json.dumps({"pid": 4242, "sessionId": SID, "name": "implementer-a"}))
    assert kit.bash("uv run pytest") is None
    assert kit.bash("claude --bg x") == "raw-starts"
