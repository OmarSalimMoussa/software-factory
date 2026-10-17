"""The owner's messages to a session: what its panel offers, and what reaches the session."""

from __future__ import annotations

import json
from typing import Any

import pytest
from dashboard_rig import Rig

from runtime.shared import store
from runtime.warden.owner import ops
from runtime.warden.owner.messages import PREFIX

THREAD = [
    {"who": "You", "at": "Oct 9, 11:30", "text": "Check Safari first: most of the reports come from it."},
    {"who": "Implementer", "at": "Oct 9, 11:30",
     "text": "Safari drops the session cookie on the redirect."},
    {"who": "Implementer", "at": "Oct 9, 11:30", "text": "A test now covers it."},
]  # fmt: skip


@pytest.mark.parametrize(
    ("session", "label", "chat_note", "thread"),
    [
        (
            "impl-sign-in-loop",
            "Send",
            "Open chat needs a full claude.ai sign-in, which the agents do not have: message it here.",
            THREAD,
        ),
        ("impl-project-names", "Bring back with this message", "On the bench: no chat until it comes back.", []),
        ("qa-ui-1", None, "A subagent runs inside its parent's session: open the parent's chat.", []),
        ("h1", None, "This session ended before its chat link appeared.", []),
    ],
)
def test_the_panel_offers_what_the_session_can_take(
    rig: Rig, session: str, label: str | None, chat_note: str, thread: list[dict[str, Any]]
) -> None:
    panel = rig.parts(f"/api/agent/{session}")["agent"]["data"]
    assert (panel["message"] or {}).get("label") == label
    assert (panel["chat_url"], panel["chat_note"]) == (None, chat_note)
    assert panel["thread"] == thread


@pytest.mark.parametrize(("session", "offered"), [("orchestrator", True), ("impl-sign-in-loop", False)])
def test_under_yolo_mode_only_the_orchestrators_panel_offers_a_message(rig: Rig, session: str, offered: bool) -> None:
    store.write_json(rig.places.state / "yolo.json", {"since": "2026-10-09T10:00:00Z", "told": []})
    panel = rig.parts(f"/api/agent/{session}")["agent"]["data"]
    assert (panel["message"] is not None) is offered


def test_a_message_reaches_the_session_as_its_next_turn(rig: Rig) -> None:
    body = {"session": "impl-sign-in-loop", "text": "Check Safari.", "idempotency_key": "message-01"}
    sent = json.loads(rig.post("/api/agent/message", body).body)
    assert (sent["ok"], sent["message"]) == (True, "Sending your message.")
    ops.join(sent["op"], 10)
    done = rig.parts(f"/api/ops/{sent['op']}")["op"]["data"]
    assert (done["state"], done["result"]["message"]) == (
        "done",
        "Sent. It reads your message as soon as its current turn ends.",
    )
    assert rig.warden.launcher.calls[-1] == ("message", ("impl-sign-in-loop", PREFIX + "Check Safari."))
    refused = rig.post("/api/agent/message", {"session": "qa-ui-1", "text": "Hi", "idempotency_key": "message-02"})
    assert refused.status == 409 and json.loads(refused.body)["refused"] == "subagent"
