"""The priority score (intake spec, "The score"): five parts, weighed into a score out of 100."""

from __future__ import annotations

import dataclasses

import pytest
from queue_kit import World

from runtime.queue.score import score
from runtime.shared.models import Request, Task

DAY = 86400


def _requests(n: int, users: int, at: str = "2026-10-09T12:00:00Z") -> list[Request]:
    out = []
    for i in range(n):
        inbox, sender, report = ("users", f"user-{i % users}", "r") if users else ("agents", "implementer", "")
        out.append(Request(f"r-2026-10-09-{i + 1:04d}", inbox, sender, at, "normal", "here", "x", report, ""))
    return out


@pytest.mark.parametrize(
    ("severity", "goal_item", "count", "users", "days", "expected"),
    [
        ("major", "G1", 1, 0, 0, 70.0),  # severity 40 + goal 30
        ("minor", "G3", 1, 0, 0, 26.0),  # 0.4 × 40 + 1/3 × 30
        ("major", "G1", 20, 0, 0, 85.0),  # + every request part
        ("major", "G1", 10, 10, 0, 87.1),  # + 9/19 of requests, + all of users
        ("major", "G1", 1, 0, 14, 75.0),  # + all of age
        ("severe", "", 1, 0, 0, 40.0),  # no goal item scores no goal part
    ],
)
def test_the_score_weighs_its_parts(
    world: World, severity: str, goal_item: str, count: int, users: int, days: int, expected: float
) -> None:
    reqs = _requests(count, users)
    task = Task("t-0001", "T", [r.id for r in reqs], goal_item, severity, ["ui"], "queued", reqs[0].at, "")
    assert score(task, reqs, 3, world.clock.now() + days * DAY, world.settings) == expected


def test_a_score_is_unknown_when_nothing_is_weighed(world: World) -> None:
    world.with_settings(intake__weights={"severity": 0, "goal": 0, "requests": 0, "users": 0, "age": 0})
    reqs = _requests(1, 0)
    task = dataclasses.replace(
        Task("t-0001", "T", [reqs[0].id], "G1", "major", ["ui"], "queued", reqs[0].at, ""), score=None
    )
    assert score(task, reqs, 3, world.clock.now(), world.settings) is None
