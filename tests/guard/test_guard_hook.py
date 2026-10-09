"""The hook's wire: the owner's sessions are never guarded, a refusal is exit 2 with one line on standard error and
one JSON line to the warden, a failing guard refuses everything but reading, and a rewrite is an allowed input."""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest
from guard_kit import NOW, SID, TESTER_ID, Kit, entry

from runtime.guard import hook
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
    assert set(record) == {"at", "session", "rule", "tool", "detail"}
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
    kit.subagent(TESTER_ID, [entry(NOW, "user", "Scenarios and checks: 1")])
    tester = {"agent_id": TESTER_ID, "agent_type": "qa-backend"}
    code, out, _ = _handle(kit, kit.payload("Bash", {"command": "sleep 900", "timeout": 600_000}, **tester))
    output = json.loads(out)["hookSpecificOutput"]
    assert code == 0 and output["permissionDecision"] == "allow"
    assert output["updatedInput"]["timeout"] == 120_000


@pytest.mark.parametrize("event", ["SubagentStart", "SubagentStop"])
def test_a_subagent_report_never_blocks_when_the_warden_is_away(kit: Kit, event: str) -> None:
    payload: dict[str, object] = {"hook_event_name": event, "session_id": SID, "agent_id": TESTER_ID}
    code, out, err = _handle(kit, payload)
    assert (code, out) == (0, "") and err.startswith("factory guard: subagent.") and "not sent" in err
