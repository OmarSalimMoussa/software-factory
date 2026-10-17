"""The owner's actions: each reaches its warden verb only while what the owner saw still holds."""

from __future__ import annotations

import json
from pathlib import Path

from dashboard_rig import Rig
from dashboard_seed import blocked

from runtime.shared import store
from runtime.warden.owner import ops

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


def test_a_blocked_task_asks_the_owner_only_to_say_it_is_fixed_and_then_goes_back_to_its_builder(
    rig: Rig, tmp_path: Path
) -> None:
    blocked(tmp_path / "repo", rig.places, "t-0020", "The seed is missing", "2026-10-09T11:00:00Z")

    card = next(c for c in rig.parts("/api/home")["needs"]["data"]["cards"] if c["id"] == "n4")
    assert (card["title"], card["sub"], [a["label"] for a in card["actions"]]) == (
        "Long project names wrap badly is blocked", "The seed is missing", ["Fixed, carry on"],
    )  # fmt: skip
    task = tmp_path / "repo" / "queue" / "tasks" / "t-0020.md"
    reply = rig.post("/api/needs/n4/fixed", {"version": card["version"], "idempotency_key": "fixed-0001"})

    assert json.loads(reply.body)["message"] == "Back to its builder, who carries on when a place frees."
    assert "status: building" in task.read_text()


def test_a_move_sends_the_warden_its_own_order_once_the_device_saw_the_same(rig: Rig) -> None:
    store.write_json(rig.places.proposals, {"open": ["t-0027", None, "t-0024", "t-0025"], "approved": []})
    card = next(s for s in rig.parts("/api/home")["proposals"]["data"]["slots"] if s.get("task") == "t-0024")
    reply = rig.post(
        "/api/proposals/t-0024/move",
        {"version": card["version"], "dir": -1, "seen_order": ["t-0027", "t-0024", "t-0025"],
         "idempotency_key": "move-0002"},
    )  # fmt: skip
    assert reply.status == 200
    assert store.read_json(rig.places.proposals)["open"] == ["t-0027", "t-0024", None, "t-0025"]


def test_the_claude_account_section_shows_no_token_and_switches_as_an_operation(rig: Rig) -> None:
    page = rig.get("/api/settings")
    rows = json.loads(page.body)["parts"]["accounts"]["data"]["rows"]
    assert b"sk-ant-oat01" not in page.body
    assert [(r["name"], r["chip"], r["token"], r["switch"] is None, r["remove"] is None) for r in rows] == [
        ("main", "active", "\u2026main", True, True),
        ("work", None, "\u2026work", False, False),
    ]
    empty = rig.post("/api/accounts", {"name": "spare", "token": "  ", "idempotency_key": "account-add-01"})
    assert (empty.status, json.loads(empty.body)["reason"]) == (400, "Paste the account's token.")
    switched = json.loads(rig.post(rows[1]["switch"]["url"], {"idempotency_key": "account-use-01"}).body)
    ops.join(switched["op"])
    op = rig.parts(f"/api/ops/{switched['op']}")["op"]["data"]
    assert (switched["message"], op["title"], op["state"]) == (
        "Switching the agents to work.",
        "Switching the Claude account",
        "done",
    )
    assert op["result"]["message"] == "The agents now use work."


def test_an_action_without_the_version_the_owner_saw_is_refused(rig: Rig) -> None:
    reply = rig.post("/api/proposals/t-0027/approve", {"idempotency_key": "approve-03"})

    assert (reply.status, json.loads(reply.body)["refused"]) == (409, "stale")
    assert store.read_json(rig.places.proposals)["approved"] == []


def test_an_entry_marked_fixed_leaves_the_open_list_for_the_fixed_one(rig: Rig) -> None:
    first = rig.parts("/api/reflect")["reflect"]["data"]
    entry = first["entries"][0]

    reply = rig.post(f"/api/reflections/{entry['id']}/mark", {"state": "fixed", "idempotency_key": "mark-0001"})

    assert json.loads(reply.body) == {"ok": True, "refresh": ["reflect"], "message": "Marked fixed."}
    after = rig.parts("/api/reflect")["reflect"]["data"]
    assert after["health"]["open"] == first["health"]["open"] - 1
    assert entry["id"] not in [e["id"] for e in after["entries"]]
    fixed = rig.parts("/api/reflect?state=fixed")["reflect"]["data"]["entries"]
    assert [(e["id"], e["state"], e["actions"]) for e in fixed] == [
        (entry["id"], "Fixed", [{"state": "open", "label": "Reopen"}])
    ]
