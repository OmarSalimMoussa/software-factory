"""The owner's actions: each reaches its warden verb only while what the owner saw still holds."""

from __future__ import annotations

import json

from dashboard_rig import Rig

from runtime.shared import store

STALE = {"ok": False, "refused": "stale", "reason": "This changed; here it is now."}


def test_an_action_on_what_changed_is_refused_and_changes_nothing(rig: Rig) -> None:
    before = rig.places.proposals.read_text()
    approve = rig.post("/api/proposals/t-0027/approve", {"version": "seen-before", "idempotency_key": "approve-01"})
    move = rig.post(
        "/api/proposals/t-0024/move",
        {"dir": -1, "seen_order": ["t-0024", "t-0027", "t-0025"], "idempotency_key": "move-0001"},
    )
    assert (approve.status, json.loads(approve.body)) == (409, STALE)
    assert (move.status, json.loads(move.body)) == (409, STALE)
    assert rig.places.proposals.read_text() == before


def test_an_approval_answers_with_its_toast_and_its_place_settles(rig: Rig) -> None:
    card = rig.parts("/api/home")["proposals"]["data"]["slots"][0]
    reply = rig.post("/api/proposals/t-0027/approve", {"version": card["version"], "idempotency_key": "approve-02"})
    assert json.loads(reply.body) == {
        "ok": True,
        "refresh": ["home", "work", "shell"],
        "message": "Approved. It starts when a slot is free.",
    }
    assert store.read_json(rig.places.proposals) == {"open": [None, "t-0024", "t-0025"], "approved": ["t-0027"]}
    slots = rig.parts("/api/home")["proposals"]["data"]["slots"]
    assert slots[0]["kind"] == "settled" and slots[0]["state"] == "approved"
    rig.clock.t += 15
    assert rig.parts("/api/home")["proposals"]["data"]["slots"][0] == {"kind": "choosing"}


def test_a_move_sends_the_warden_its_own_order_once_the_device_saw_the_same(rig: Rig) -> None:
    store.write_json(rig.places.proposals, {"open": ["t-0027", None, "t-0024", "t-0025"], "approved": []})
    reply = rig.post(
        "/api/proposals/t-0024/move",
        {"dir": -1, "seen_order": ["t-0027", "t-0024", "t-0025"], "idempotency_key": "move-0002"},
    )
    assert reply.status == 200
    assert store.read_json(rig.places.proposals)["open"] == ["t-0027", "t-0024", None, "t-0025"]
