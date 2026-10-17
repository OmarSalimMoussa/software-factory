"""Work's Done tab: a finished task moves there from Tasks, and its button opens slot 0 only when it can."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from dashboard_rig import Rig

from runtime.shared import settings, store
from runtime.warden.delivery import done


@pytest.mark.parametrize(
    ("listed", "reverted", "on_done"),
    [(True, False, True), (False, False, False), (True, True, False)],
    ids=["done-lists-it", "not-read-yet", "reverted-since"],
)
def test_a_task_is_on_done_or_on_tasks_never_both(
    rig: Rig, tmp_path: Path, listed: bool, reverted: bool, on_done: bool
) -> None:
    if not listed:
        done.path(rig.places).unlink()
    if reverted:  # the revert sent it back to building before the warden read the branch again
        task = tmp_path / "repo" / "queue" / "tasks" / "t-0013.md"
        task.write_text(task.read_text().replace("status: on-staging", "status: building"))

    groups = rig.parts("/api/work?tab=tasks")["tasks"]["data"]["groups"]
    rows = rig.parts("/api/work?tab=done")["done"]["data"]["rows"]

    assert ("t-0013" in {row["id"] for row in rows}, "t-0013" in {r["id"] for g in groups for r in g["rows"]}) == (
        on_done,
        not on_done,
    )


@pytest.mark.parametrize(
    ("slot0", "address", "label", "opens"),
    [
        ({"commit": "a1b2c3d", "behind": 0, "why": None}, "owner@example.com", "Open acme-app", True),
        ({"commit": "a1b2c3d", "behind": 0, "why": None}, "", "Open acme-app", False),
        ({"commit": None, "behind": None, "why": "bring it up"}, "owner@example.com", "Slot 0 is down", False),
    ],
    ids=["ready", "no-address", "slot-down"],
)
def test_the_open_button_says_why_it_cannot_open_slot_0(
    rig: Rig, slot0: dict[str, Any], address: str, label: str, opens: bool
) -> None:
    store.write_json(done.path(rig.places), {**store.read_json(done.path(rig.places)), "slot0": slot0})
    rig.places.overrides.write_text(settings.overrides_toml({"slots": {"owner_address": address}}))

    button = rig.parts("/api/work?tab=done")["done"]["data"]["master"]["button"]

    assert (button["label"], button["url"] is not None) == (label, opens)


def test_open_answers_with_the_link_the_page_opens(rig: Rig) -> None:
    reply = rig.post("/api/slot/open", {"idempotency_key": "open-slot-01"})

    assert json.loads(reply.body)["open"] == "http://203.0.113.7:8090/sign-in/once"
