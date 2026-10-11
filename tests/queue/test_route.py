"""The dispatch ladder (routing spec): the lowest rung whose every condition holds."""

from __future__ import annotations

import dataclasses

import pytest
from queue_kit import World

from runtime.queue import route
from runtime.queue import verbs as queue_verbs
from runtime.shared.models import Task
from runtime.shared.protocol import Refused
from runtime.warden import inbox

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


def test_an_implementers_cause_tries_are_counted_and_the_last_tells_the_orchestrator(world: World) -> None:
    task = world.task()
    world.session("s-impl", "implementer", "implementer-t-0001", task=task.id)
    ctx = world.ctx(role="implementer", session="s-impl")
    limit = world.settings.get_int("routing.cause_tries")

    answers = [queue_verbs.cause_try(ctx, {"tried": f"idea {n}: the log showed otherwise"}, None) for n in range(limit)]

    assert [a["tries_left"] for a in answers] == list(range(limit - 1, -1, -1))
    assert [kind for kind, _ in world.events] == [inbox.NEEDS_RUNG]
    with pytest.raises(Refused, match="tries are spent"):
        queue_verbs.cause_try(ctx, {"tried": "one more"}, None)
    logged = [e for e in world.queue().read_log(task.id) if e["event"] == queue_verbs.CAUSE_TRY]
    assert len(logged) == limit and logged[0]["text"] == "idea 0: the log showed otherwise"
