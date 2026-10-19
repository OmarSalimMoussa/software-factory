"""A change to the goal items of GOAL.md brings every parked request and task back in one write, and the next
triage judges each task against the new goals before it can be proposed."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from queue_kit import GOAL, ITEMS, PICTURE, World, goal_text, plan, set_outcome

from runtime.queue import goal, owner, tasks, triage
from runtime.queue.files import QueueFiles
from runtime.queue.propose import propose
from runtime.shared import store
from runtime.shared.models import Task
from runtime.shared.protocol import Refused

G1, G2, G3 = ITEMS


def _change(world: World, text: str) -> dict[str, Any] | None:
    (world.repo / "GOAL.md").write_text(text)
    return owner.goals_changed(world.ctx(), text)


def _back(world: World) -> Task:
    """A parked task for G1, back in the queue since G1 was marked done."""
    task = world.task()
    tasks.set_status(world.ctx(), task.id, "parked", "", "")
    _change(world, GOAL)
    _change(world, goal_text(f"[x] {G1}", G2, G3))
    return task


@pytest.mark.parametrize(
    ("items", "who", "said"),
    [
        ((G1, G2, G3, "G4 Search works. Done when it does."), "People.", ["G4 added"]),
        ((G1, G3), "People.", ["G2 removed"]),
        ((G1, "G2 Pages load fast. Done when they do.", G3), "People.", ["G2 reworded"]),
        ((G1, G2, f"[x] {G3}"), "People.", ["G3 marked done"]),
        ((G3, G1, G2), "People.", ["G3 re-ranked"]),
        (("G0 Search works. Done when it does.", G1, G2, G3), "People.", ["G0 added"]),
        ((G1, G2, G3), "Busy people.", []),
    ],
    ids=["added", "removed", "reworded", "done", "re-ranked", "added-above", "other-section"],
)
def test_only_a_change_to_the_goal_items_counts_and_each_is_named(
    items: tuple[str, ...], who: str, said: list[str]
) -> None:
    assert goal.changes(goal.parse(GOAL), goal.parse(goal_text(*items, who=who))) == said


def test_a_goal_item_change_brings_every_parked_item_back_in_one_write_and_records_its_cause(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    parked, noise = world.request(), world.request()
    set_outcome(world, parked, "parked serves no goal item")
    set_outcome(world, noise, "noise a test")
    task = world.task()
    tasks.set_status(world.ctx(), task.id, "parked", "", "")
    _change(world, GOAL)
    writes: list[list[str]] = []
    real = QueueFiles.write_many
    monkeypatch.setattr(QueueFiles, "write_many", lambda self, files: writes.append(sorted(files)) or real(self, files))

    back = _change(world, goal_text(G1, "G2 Pages load fast. Done when they do.", G3))

    assert back == {"at": world.clock.iso(), "changed": ["G2 reworded"], "requests": [parked.id], "tasks": [task.id]}
    assert writes == [sorted([f"requests/agents/{parked.id}.md", f"tasks/{task.id}.md"])]
    queue = world.queue()
    assert [queue.request(r.id).outcome for r in (parked, noise)] == ["", "noise a test"]
    assert queue.task(task.id).status == "queued"
    last = queue.read_log(task.id)[-1]
    assert (last["event"], last["text"]) == ("goals-changed", "goals changed: G2 reworded")
    assert store.read_jsonl(owner.goal_changes_path(world.places)) == [back]


def test_the_first_reading_and_an_edit_outside_the_goal_items_bring_nothing_back(world: World) -> None:
    request = world.request()
    set_outcome(world, request, "parked serves no goal item")

    assert _change(world, goal_text(G1, G2, G3, who="Busy people.")) is None
    assert _change(world, GOAL) is None

    assert world.queue().request(request.id).outcome == "parked serves no goal item"


def test_a_task_back_from_parked_is_triaged_at_once_and_proposed_only_once_triage_kept_it(world: World) -> None:
    task = _back(world)
    world.request()

    with pytest.raises(Refused) as refused:
        propose(world.ctx(), task.id, 1, "why", "", PICTURE, "")
    assert refused.value.rule == "returned"
    assert tasks.next_up(world.ctx())["skipped"] == [{"task": task.id, "why": "waits for triage"}]
    found = triage.batch(world.ctx())
    assert (found.ids[0], found.due) == (task.id, True)

    triage.apply(world.ctx(), plan(batch=[task.id], kept={task.id: "G2"}))
    assert propose(world.ctx(), task.id, 1, "why", "", PICTURE, "")["task"] == task.id


@pytest.mark.parametrize(
    ("judged", "status", "goal_item", "said"),
    [
        (lambda t: {"kept": {t: "G2"}}, "queued", "G2", "kept for G2 under the new goals"),
        (lambda t: {"parked": {t: "serves no goal item now"}}, "parked", "G1", "serves no goal item now"),
    ],
    ids=["kept", "parked-again"],
)
def test_triage_keeps_a_task_back_from_parked_for_an_open_goal_item_or_parks_it_again(
    world: World, judged: Callable[[str], dict[str, Any]], status: str, goal_item: str, said: str
) -> None:
    task = _back(world)

    triage.apply(world.ctx(), plan(batch=[task.id], **judged(task.id)))

    found = world.queue().task(task.id)
    assert (found.status, found.goal_item, world.queue().read_log(task.id)[-1]["text"]) == (status, goal_item, said)
    assert triage.batch(world.ctx()).ids == []


@pytest.mark.parametrize(
    ("judged", "says"),
    [
        (lambda t, other, r: plan(batch=[t], kept={t: "G1"}), "no open item"),
        (lambda t, other, r: plan(batch=[t], noise={t: "a test"}), "cannot be noise"),
        (lambda t, other, r: plan(batch=[t, r], parked={t: "x"}, kept={r: "G2"}), "cannot be kept"),
        (lambda t, other, r: plan(batch=[t, other], kept={t: "G2", other: "G2"}), "no task back from parked"),
    ],
    ids=["done-goal-item", "task-as-noise", "request-kept", "task-not-back"],
)
def test_a_plan_that_misjudges_a_task_back_from_parked_writes_nothing(
    world: World, judged: Callable[[str, str, str], bytes], says: str
) -> None:
    task, other, request = _back(world), world.task(), world.request()

    with pytest.raises(Refused, match=says):
        triage.apply(world.ctx(), judged(task.id, other.id, request.id))

    assert (world.queue().task(task.id).status, triage.batch(world.ctx()).ids[0]) == ("queued", task.id)
