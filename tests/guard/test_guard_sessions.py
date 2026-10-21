"""Who is calling decides what is left: the owner's own session, a session the warden never started, a retired one,
a stopped subagent, a session being benched; and what a session reads at start and needs before it goes idle."""

from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from guard_kit import NOW, SID, TESTER_ID, Kit, agent_use, entry, result

from runtime.guard import deadline, decide, history, transcript
from runtime.guard.facts import Refusal
from runtime.shared import lanes, store
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
    ("file", "refused"), [(".factory/a1b2c3/f-1.md", False), ("app/notes.py", True)], ids=["its-report", "the-code"]
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


_PARSED = transcript.parsed


def _finished(count: int) -> list[str]:
    started = NOW - timedelta(hours=1)
    pairs = [([agent_use(f"f{n}", "intake")], [result(f"f{n}", "Intake: done.")]) for n in range(count)]
    return [line for use, done in pairs for line in (entry(started, "assistant", use), entry(started, "user", done))]


def _two_running() -> list[str]:
    return [entry(NOW, "assistant", [agent_use("r0", "qa-regression"), agent_use("r1", "intake")])]


def _parses(monkeypatch: pytest.MonkeyPatch, cut_at: int = 0) -> list[bytes]:
    """The transcript lines each decision parses from here on; the ``cut_at``-th runs the guard out of time."""
    read: list[bytes] = []

    def counted(line: bytes) -> dict[str, Any] | None:
        read.append(line)
        if len(read) == cut_at:
            raise deadline.OutOfTime("the guard ran out of time to decide")
        return _PARSED(line)

    monkeypatch.setattr(transcript, "parsed", counted)
    return read


def test_a_long_sessions_subagent_start_parses_only_the_lines_added_since_the_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kit = Kit(tmp_path)
    kit.session("orchestrator", worktree=kit.repo)
    kit.settings(caps__subagents_at_once=3)
    kit.transcript(_finished(2000))
    assert kit.rule("Agent", {"subagent_type": "triage", "prompt": "Triage these."}) is None
    read = _parses(monkeypatch)
    kit.transcript([*_finished(2000), *_two_running()])
    assert kit.rule("Agent", {"subagent_type": "triage", "prompt": "Triage these."}) == "subagents-at-once"
    assert len(read) == 1


def test_a_start_cut_short_by_the_guards_time_spares_the_next_the_steps_it_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kit = Kit(tmp_path)
    kit.session("orchestrator", worktree=kit.repo)
    kit.settings(caps__subagents_at_once=3)
    lines = [*_finished(400), *_two_running()]
    kit.transcript(lines)
    monkeypatch.setattr(history, "_STEP", 10_000)
    _parses(monkeypatch, cut_at=300)
    with pytest.raises(deadline.OutOfTime):
        kit.decide("Agent", {"subagent_type": "triage", "prompt": "Triage these."})
    read = _parses(monkeypatch)
    assert kit.rule("Agent", {"subagent_type": "triage", "prompt": "Triage these."}) == "subagents-at-once"
    assert 0 < len(read) <= len(lines) - 200  # on from the last step it kept, of 300 lines read, never from the start


@pytest.mark.parametrize("kept", ["{", '{"at": 1}', None], ids=["unreadable", "misshapen", "another-transcripts"])
def test_a_kept_read_that_does_not_fit_the_transcript_is_read_again_from_its_start(
    tmp_path: Path, kept: str | None
) -> None:
    kit = Kit(tmp_path)
    kit.session("orchestrator", worktree=kit.repo)
    kit.settings(caps__subagents_at_once=3)
    kit.transcript(_two_running())
    assert kit.rule("Agent", {"subagent_type": "triage", "prompt": "Triage these."}) == "subagents-at-once"
    if kept is not None:
        (kit.session_transcript.with_suffix("") / history.KEPT).write_text(kept)
    kit.transcript(_finished(20))
    assert kit.rule("Agent", {"subagent_type": "triage", "prompt": "Triage these."}) is None


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


@pytest.mark.parametrize(
    ("waiting", "told", "refused"), [([SID], {}, False), ([], {SID: 1.0}, True), (["other"], {}, True)],
    ids=["waits-for-a-browser-place", "was-told-of-one-already", "another-waits"],
)  # fmt: skip
def test_an_implementer_waiting_for_a_browser_place_may_stop_as_the_warden_tells_it(
    tmp_path: Path, waiting: list[str], told: dict[str, float], refused: bool
) -> None:
    kit = Kit(tmp_path)
    kit.session("implementer")
    kit.transcript([_PROMPT])
    lanes.write_line(kit.places, waiting, told)
    payload = {"hook_event_name": "Stop", "session_id": SID, "stop_hook_active": False, "cwd": str(kit.worktree),
               "transcript_path": str(kit.session_transcript)}  # fmt: skip
    call = decide.resolve(payload, kit.places, NOW, kit.home)
    assert call is not None
    decision = decide.check_stop(call)
    assert (isinstance(decision, Refusal) and decision.rule == "no-waker") is refused


@pytest.mark.parametrize(
    ("minutes", "refused"), [(10, False), (-1, True), (None, True)],
    ids=["an-upgrade-holds-testers", "the-hold-ran-out", "no-upgrade"],
)  # fmt: skip
def test_an_implementer_an_upgrade_keeps_from_its_tester_may_stop_as_the_resume_wakes_it(
    tmp_path: Path, minutes: int | None, refused: bool
) -> None:
    kit = Kit(tmp_path)
    kit.session("implementer")
    kit.transcript([_PROMPT])
    if minutes is not None:
        store.write_json(kit.places.upgrade_hold, {"until": (NOW + timedelta(minutes=minutes)).isoformat()})
    payload = {"hook_event_name": "Stop", "session_id": SID, "stop_hook_active": False, "cwd": str(kit.worktree),
               "transcript_path": str(kit.session_transcript)}  # fmt: skip
    call = decide.resolve(payload, kit.places, NOW, kit.home)
    assert call is not None
    decision = decide.check_stop(call)
    assert (isinstance(decision, Refusal) and decision.rule == "no-waker") is refused


@pytest.mark.parametrize(
    ("status", "refused"), [("qa-clean", False), ("merged", False), ("turn", True), ("building", True)]
)
def test_an_implementer_whose_task_waits_for_its_merge_turn_or_check_may_stop_as_the_warden_wakes_it(
    tmp_path: Path, status: str, refused: bool
) -> None:
    kit = Kit(tmp_path)
    kit.session("implementer")
    kit.task(status)
    kit.transcript([_PROMPT, _TO_ORCH, _SENT, _ORCH_SAYS])
    payload = {"hook_event_name": "Stop", "session_id": SID, "stop_hook_active": False, "cwd": str(kit.worktree),
               "transcript_path": str(kit.session_transcript)}  # fmt: skip
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
