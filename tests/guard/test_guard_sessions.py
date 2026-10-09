"""Who is calling decides what is left: the owner's own session, a session the warden never started, a retired one,
a stopped subagent, a session being benched; and what a session reads at start and needs before it goes idle."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from guard_kit import NOW, SID, TESTER_ID, Kit

from runtime.guard import decide
from runtime.guard.facts import PENDING, Refusal
from runtime.warden import launch


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


def test_every_call_of_a_stopped_subagent_is_refused_and_its_siblings_run_on(tmp_path: Path) -> None:
    kit = Kit(tmp_path)
    kit.session("implementer")
    kit.session("implementer", sid="sub-1", state="retired", agent_id=TESTER_ID, parent=SID)
    kit.subagent(TESTER_ID, [])
    stopped = {"agent_id": TESTER_ID, "agent_type": "Explore"}
    assert kit.rule("Read", {"file_path": "/w/app.py"}, **stopped) == "stopped-subagent"
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


def test_a_session_the_warden_has_not_bound_yet_is_known_by_the_name_it_was_started_under(tmp_path: Path) -> None:
    kit = Kit(tmp_path)
    kit.session("implementer", sid=launch.PENDING + "0f0f")
    # Claude Code's own record of the running session pairs the id the hook sees with the name the warden gave it.
    records = kit.home / ".claude" / "sessions"
    records.mkdir(parents=True)
    (records / "4242.json").write_text(json.dumps({"pid": 4242, "sessionId": SID, "name": "implementer-a"}))
    assert PENDING == launch.PENDING
    assert kit.bash("uv run pytest") is None
    assert kit.bash("claude --bg x") == "raw-starts"
