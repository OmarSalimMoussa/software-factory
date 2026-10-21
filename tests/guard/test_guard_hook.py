"""The hook's wire: the owner's sessions are never guarded, a refusal is exit 2 with one line on standard error and
one JSON line to the warden, a failing guard refuses everything but reading, and a rewrite is an allowed input."""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from guard_kit import NOW, SID, TESTER_ID, Kit, entry

from runtime.guard import hook, wire
from runtime.shared.paths import SLICE

IN_SLICE = f"0::/{SLICE}/agent-run@{SID}.service\n"
OWNERS = "0::/user.slice/user-1000.slice/session-2.scope\n"


@pytest.fixture
def kit(tmp_path: Path) -> Kit:
    made = Kit(tmp_path)
    made.session("implementer")
    return made


@pytest.fixture
def refusals(kit: Kit) -> Iterator[socket.socket]:
    kit.places.refusal_socket.parent.mkdir(parents=True)
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.bind(str(kit.places.refusal_socket))
        sock.settimeout(2)
        yield sock


def _handle(kit: Kit, payload: dict[str, object], cgroup: str = IN_SLICE) -> tuple[int, str, str]:
    return hook.handle(json.dumps(payload), cgroup, kit.places, kit.home, NOW)


def test_a_session_outside_the_slice_is_the_owners_and_never_guarded(kit: Kit) -> None:
    assert _handle(kit, kit.payload("Bash", {"command": "sudo ls"}), OWNERS) == (0, "", "")


def test_a_refusal_is_one_line_to_the_agent_and_one_json_line_to_the_warden(kit: Kit, refusals: socket.socket) -> None:
    code, out, err = _handle(kit, kit.payload("Bash", {"command": "claude --bg x"}))
    assert (code, out) == (2, "")
    assert err.startswith("Refused by the factory guard (raw-starts): ") and "\n" not in err
    line = refusals.recv(65536).decode()
    assert line.endswith("\n")
    record = json.loads(line)
    assert set(record) == {"at", *wire.REFUSAL_FIELDS}
    assert (record["at"], record["session"], record["rule"], record["tool"]) == (
        "2026-10-09T12:00:00Z",
        SID,
        "raw-starts",
        "Bash",
    )


def test_a_guard_that_cannot_read_its_settings_refuses_all_but_reading(kit: Kit) -> None:
    (kit.public / "settings.json").write_text("{not json")
    code, _, err = _handle(kit, kit.payload("Bash", {"command": "ls"}))
    assert code == 2 and err.startswith("Refused by the factory guard (guard): the guard failed (StoreError")
    assert _handle(kit, kit.payload("Read", {"file_path": "/x"}))[0] == 0
    _, out, _ = _handle(kit, {"hook_event_name": "SessionStart", "session_id": SID})
    assert "the guard failed" in json.loads(out)["hookSpecificOutput"]["additionalContext"]


def test_a_rewrite_is_an_allowed_call_with_its_new_input(kit: Kit) -> None:
    kit.settings(qa__setup_seconds=30, qa__seconds_per_item=120)
    kit.subagent(TESTER_ID, [entry(NOW, "user", "Scenarios and checks: 1")])
    tester = {"agent_id": TESTER_ID, "agent_type": "qa-backend"}
    code, out, _ = _handle(kit, kit.payload("Bash", {"command": "sleep 900", "timeout": 600_000}, **tester))
    output = json.loads(out)["hookSpecificOutput"]
    assert code == 0 and output["permissionDecision"] == "allow"
    assert output["updatedInput"]["timeout"] == 150_000


@pytest.mark.parametrize("event", ["SubagentStart", "SubagentStop"])
def test_a_subagent_report_never_blocks_when_the_warden_is_away(kit: Kit, event: str) -> None:
    payload: dict[str, object] = {"hook_event_name": event, "session_id": SID, "agent_id": TESTER_ID}
    code, out, err = _handle(kit, payload)
    assert (code, out) == (0, "") and err.startswith("factory guard: subagent.") and "not sent" in err


def test_a_guard_that_runs_out_of_its_time_refuses_the_call_instead_of_letting_it_through(
    kit: Kit, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runtime.guard import deadline, decide

    def slow(call: object) -> None:
        time.sleep(5)

    monkeypatch.setattr(decide, "decide_tool", slow)

    def handled(payload: dict[str, object]) -> tuple[int, str, str]:
        deadline.arm(0.4)
        try:
            return _handle(kit, payload)
        finally:
            deadline.disarm()

    started = time.monotonic()
    code, _, err = handled(kit.payload("Bash", {"command": "ls"}))
    assert time.monotonic() - started < 2
    assert code == 2 and "ran out of time" in err
    assert handled(kit.payload("Read", {"file_path": "/x"}))[0] == 0


def _asked_warden(kit: Kit, payload: dict[str, object]) -> tuple[tuple[int, str, str], list[tuple[object, object]]]:
    """What the hook answered ``payload``, and each request it sent the warden meanwhile, which all answer ok."""
    kit.places.socket.parent.mkdir(parents=True, exist_ok=True)
    asked: list[tuple[object, object]] = []
    handled = threading.Event()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(kit.places.socket))
        server.listen(1)
        server.settimeout(0.05)

        def answer() -> None:
            while not handled.is_set():
                try:
                    conn, _ = server.accept()
                except TimeoutError:
                    continue
                with conn:
                    request = json.loads(conn.makefile("rb").readline())
                    asked.append((request["verb"], request["args"]))
                    conn.sendall(b'{"ok": true}\n')

        warden = threading.Thread(target=answer)
        warden.start()
        answered = _handle(kit, payload)
        handled.set()
        warden.join(5)
    return answered, asked


HELPER_ID = "acf8774bffcf0ef6d"


@pytest.mark.parametrize(
    ("agent_id", "agent_type", "asked"),
    [
        # The lane goes as started, so the tester's evidence is its own lane's runs.
        (TESTER_ID, "qa-ux-2", [("subagent.start", {"agent_id": TESTER_ID, "kind": "qa-ux-2"})]),
        # Claude Code's progress summary of a subagent runs on a copy of it, under a new id typed as the session.
        (HELPER_ID, "implementer", []),
    ],
)
def test_a_subagent_the_warden_never_registered_is_registered_on_its_calls_but_never_claude_codes_own_helper(
    kit: Kit, agent_id: str, agent_type: str, asked: list[tuple[object, object]]
) -> None:
    kit.subagent(TESTER_ID, [entry(NOW, "user", "Scenarios and checks: 1")])
    payload = kit.payload("Read", {"file_path": "/x"}, agent_id=agent_id, agent_type=agent_type)
    assert _asked_warden(kit, payload) == ((0, "", ""), asked)


STOPPED_ID = "a9ab8a4d065092e07"
STOP = {"tool_name": "TaskStop", "tool_input": {"task_id": STOPPED_ID}, "tool_response": {
    "message": f"Successfully stopped task: {STOPPED_ID} (UI function test of live view size)", "task_id": STOPPED_ID,
    "task_type": "local_agent", "command": "UI function test of live view size"}}  # fmt: skip
SHELL_STOP = {**STOP, "tool_response": {**STOP["tool_response"], "task_id": "b7z5rdm20", "task_type": "local_bash"}}


@pytest.mark.parametrize(
    ("event", "extra", "ended"),
    [
        ("SubagentStop", {"agent_id": STOPPED_ID}, "finished"),
        ("PostToolUse", STOP, "stopped"),
        ("PostToolUse", SHELL_STOP, None),
        ("Stop", {"background_tasks": []}, "crashed"),
        ("Stop", {"background_tasks": [{"id": STOPPED_ID, "type": "subagent", "status": "running"}]}, None),
    ],
)
def test_the_warden_hears_of_a_subagent_that_ended_however_it_ended(
    kit: Kit, event: str, extra: dict[str, object], ended: str | None
) -> None:
    kit.session("qa-ui-function", sid=STOPPED_ID, agent_id=STOPPED_ID, parent=SID)
    payload = {"hook_event_name": event, "session_id": SID, "transcript_path": str(kit.session_transcript), **extra}

    _, asked = _asked_warden(kit, payload)

    assert asked == ([("subagent.end", {"agent_id": STOPPED_ID, "ended": ended})] if ended else [])
