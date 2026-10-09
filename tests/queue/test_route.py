"""The dispatch ladder (routing spec): the lowest rung whose every condition holds."""

from __future__ import annotations

import dataclasses

import pytest
from queue_kit import World

from runtime.queue import route
from runtime.shared.models import Task
from runtime.shared.protocol import Refused

TASK = Task("t-0001", "T", ["r-2026-10-09-0001"], "G1", "major", ["ui"], "queued", "2026-10-09T12:00:00Z", "",
            cause="web/auth.py:41", slices=1)  # fmt: skip


@pytest.mark.parametrize(
    ("facts", "rung"),
    [
        ({"slices": 0}, 0),
        ({}, 1),
        ({"cause": "the session cookie is dropped"}, 2),  # known, but not to the line
        ({"surfaces": ["ui", "access"]}, 2),
        ({"slices": 3}, 2),
        ({"slices": 4}, 3),
        ({"cause": ""}, 3),
        ({"owner_class": "migration"}, 3),
        ({"owner_class": "new-service", "slices": 1}, 3),
        ({"owner_class": "large-visible"}, 4),
        ({"owner_class": "large-backend", "slices": 6}, 4),
    ],
)
def test_route_names_the_lowest_rung_that_holds(world: World, facts: dict[str, object], rung: int) -> None:
    assert route.route(dataclasses.replace(TASK, **facts), world.settings) == rung


def test_the_numbers_are_settings(world: World) -> None:
    world.with_settings(routing__small_max_slices=2)
    assert route.route(dataclasses.replace(TASK, slices=2), world.settings) == 1


def test_a_task_without_its_facts_is_not_routed(world: World) -> None:
    with pytest.raises(Refused, match="slice estimate"):
        route.route(dataclasses.replace(TASK, slices=None), world.settings)
