"""The owner's messages to a session: what its panel offers, and what reaches the session."""

from __future__ import annotations

import json
from typing import Any

import pytest
from dashboard_rig import Rig

from runtime.warden.messages import PREFIX

THREAD = [
    {"who": "You", "at": "Oct 9, 11:30", "text": "Check Safari first: most of the reports come from it."},
    {"who": "Implementer", "at": "Oct 9, 11:30",
     "text": "Safari drops the session cookie on the redirect."},
    {"who": "Implementer", "at": "Oct 9, 11:30", "text": "A test now covers it."},
]  # fmt: skip


@pytest.mark.parametrize(
    ("session", "label", "chat_note", "thread"),
    [
        ("impl-sign-in-loop", "Send", "The chat link appears a few seconds after the session starts.", THREAD),
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


def test_a_message_reaches_the_session_as_its_next_turn(rig: Rig) -> None:
    body = {"session": "impl-sign-in-loop", "text": "Check Safari.", "idempotency_key": "message-01"}
    sent = rig.post("/api/agent/message", body)
    said = "Sent. It reads your message as soon as its current turn ends."
    assert json.loads(sent.body) == {"ok": True, "refresh": ["agent", "home"], "message": said}
    assert rig.warden.launcher.calls[-1] == ("message", ("impl-sign-in-loop", PREFIX + "Check Safari."))
    refused = rig.post("/api/agent/message", {"session": "qa-ui-1", "text": "Hi", "idempotency_key": "message-02"})
    assert refused.status == 409 and json.loads(refused.body)["refused"] == "subagent"
