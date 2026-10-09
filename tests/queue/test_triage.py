"""`factory triage apply`: every request in the batch gets exactly one outcome, and the plan is written whole or
not at all."""

from __future__ import annotations

import pytest
from queue_kit import World, plan

from runtime.queue import owner, triage
from runtime.shared.protocol import Refused


def _new(*requests: str, **facts: object) -> dict[str, object]:
    return {
        "title": "Sign-in loops",
        "body": "Fix it.",
        "goal_item": "G1",
        "severity": "major",
        "surfaces": ["ui"],
        "cause": "",
        "slices": 1,
        "owner_class": "none",
        "requests": list(requests),
        "after": [],
        **facts,
    }


def test_a_plan_is_applied_whole(world: World) -> None:
    joined_to = world.task()
    a, b, c, d = (world.request() for _ in range(4))
    summary = triage.apply(
        world.ctx(),
        plan(
            batch=[a.id, b.id, c.id, d.id],
            new_tasks=[_new(a.id)],
            joins={b.id: joined_to.id},
            noise={c.id: "a test"},
            parked={d.id: "serves no goal item"},
        ),
    )
    queue = world.queue()
    made = queue.task(summary["new"][0])
    assert (made.status, made.requests, made.score is not None) == ("queued", [a.id], True)
    assert queue.task(joined_to.id).requests[-1] == b.id
    assert [queue.request(r).outcome for r in (a.id, b.id, c.id, d.id)] == [
        f"task {made.id}",
        f"joined {joined_to.id}",
        "noise a test",
        "parked serves no goal item",
    ]


@pytest.mark.parametrize(
    "broken",
    [
        lambda a, b: plan(batch=[a, b], new_tasks=[_new(a)], noise={a: "twice", b: "x"}),  # two outcomes
        lambda a, b: plan(batch=[a, b], new_tasks=[_new(a)]),  # b has none
        lambda a, b: plan(batch=[a], new_tasks=[_new(a, b)]),  # b is not in the batch
        lambda a, b: plan(batch=[a, b], new_tasks=[_new(a, goal_item="G9")], noise={b: "x"}),  # no such goal item
        lambda a, b: plan(batch=[a, b], new_tasks=[_new(a, goal_item="")], noise={b: "x"}),  # no goal item
        lambda a, b: plan(batch=[a, b], new_tasks=[_new(a)], joins={b: "t-0099"}),  # no such task
    ],
)
def test_a_broken_plan_writes_nothing(world: World, broken) -> None:  # type: ignore[no-untyped-def]
    a, b = world.request(), world.request()
    with pytest.raises(Refused) as refused:
        triage.apply(world.ctx(), broken(a.id, b.id))
    assert refused.value.rule == "triage-plan"
    queue = world.queue()
    assert queue.tasks()[0] == []
    assert [queue.request(r).outcome for r in (a.id, b.id)] == ["", ""]


def test_a_boosted_request_makes_a_boosted_task(world: World) -> None:
    a = world.request()
    owner.boost_request(world.owner(), a.id)
    summary = triage.apply(world.ctx(), plan(batch=[a.id], new_tasks=[_new(a.id)]))
    assert world.queue().task(summary["new"][0]).boosted_at == world.ctx().clock.iso()
    assert ("boosted", {"request": a.id}) in world.events
