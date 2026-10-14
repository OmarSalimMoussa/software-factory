"""Work's Done tab: a finished task moves there from Tasks, and its button opens slot 0 only when it can."""

from __future__ import annotations

import json
from typing import Any

import pytest
from dashboard_rig import Rig

from runtime.shared import settings, store
from runtime.warden.delivery import done


@pytest.mark.parametrize("listed", [True, False], ids=["done-lists-it", "not-read-yet"])
def test_a_task_leaves_the_tasks_tab_once_done_lists_it(rig: Rig, listed: bool) -> None:
    if not listed:
        done.path(rig.places).unlink()

    groups = rig.parts("/api/work?tab=tasks")["tasks"]["data"]["groups"]

    assert ("t-0013" in {row["id"] for g in groups for row in g["rows"]}) is not listed


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

    assert json.loads(reply.body)["open"] == "http://127.0.0.1:8100/sign-in/once"
