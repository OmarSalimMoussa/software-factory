"""The priority score (intake spec, "The score"): five parts, weighed into a score out of 100."""

from __future__ import annotations

import dataclasses

import pytest
from queue_kit import World

from runtime.queue import goal
from runtime.queue.score import score
from runtime.shared import order
from runtime.shared.models import Request, Task

DAY = 86400


def _requests(n: int, users: int, at: str = "2026-10-09T12:00:00Z") -> list[Request]:
    out = []
    for i in range(n):
        inbox, sender, report = ("users", f"user-{i % users}", "r") if users else ("agents", "implementer", "")
        out.append(Request(f"r-2026-10-09-{i + 1:04d}", inbox, sender, at, "normal", "here", "x", report, ""))
    return out


GOALS = {"G1": 1, "G2": 2, "G3": 3}  # the open goal items' ranks


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
    assert score(task, reqs, GOALS, world.clock.now() + days * DAY, world.settings) == expected


def test_a_score_is_unknown_when_nothing_is_weighed(world: World) -> None:
    world.with_settings(intake__weights={"severity": 0, "goal": 0, "requests": 0, "users": 0, "age": 0})
    reqs = _requests(1, 0)
    task = dataclasses.replace(
        Task("t-0001", "T", [reqs[0].id], "G1", "major", ["ui"], "queued", reqs[0].at, ""), score=None
    )
    assert score(task, reqs, GOALS, world.clock.now(), world.settings) is None


GOAL_MD = """# Goal

## The goal

- [x] G-sign-in Anyone with an invite signs in. Done when a teammate signs in.
- G-search People find a note. Done when a word lists only the notes holding it.
- [ ] G-export Reports export. Done when a report downloads.

## How it looks and feels
"""


def test_goal_items_keep_their_names_and_rank_by_their_place_among_the_open_ones() -> None:
    items = goal.parse(GOAL_MD)
    assert [(i.id, i.done, i.rank) for i in items] == [
        ("G-sign-in", True, None), ("G-search", False, 1), ("G-export", False, 2),
    ]  # fmt: skip
    moved = goal.parse(
        GOAL_MD.replace("- G-search", "- G-temp")
        .replace("- [ ] G-export", "- G-search")
        .replace("- G-temp", "- [ ] G-export")
    )  # the owner swaps two lines: each task keeps its own item
    assert goal.ranks(moved) == {"G-export": 1, "G-search": 2}
    tasks = [Task(f"t-000{n}", "T", [], item, "major", ["ui"], "queued", f"2026-10-0{n}T00:00:00Z", "")
             for n, item in enumerate(("G-search", "G-export", "G-sign-in"), 1)]  # fmt: skip
    ordered = order.ordered(tasks, order.Ranking([], goal.ranks(moved)))
    assert [t.goal_item for t in ordered] == ["G-export", "G-search", "G-sign-in"]  # a done item ranks last


@pytest.mark.parametrize(
    ("change", "says"),
    [(("G-export", "G-search"), "names G-search twice"), (("- G-search", "- 3. Search"), "does not read")],
)
def test_a_goal_list_that_cannot_be_read_is_refused_with_why(change: tuple[str, str], says: str) -> None:
    with pytest.raises(goal.GoalError, match=says):
        goal.parse(GOAL_MD.replace(*change))
