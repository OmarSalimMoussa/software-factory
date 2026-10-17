"""Approvals bind to what was approved (R20): any change to the title, rung, model or picture voids one."""

from __future__ import annotations

import dataclasses

import pytest
from queue_kit import PICTURE, World

from runtime.queue import approvals, owner, proposals, tasks
from runtime.queue.propose import propose
from runtime.queue.statuses import WARDEN


def _approved(world: World) -> str:
    task = world.task()
    propose(world.ctx(), task.id, 1, "first visit", "", PICTURE, "")
    owner.approve(world.owner(), task.id)
    return task.id


@pytest.mark.parametrize(
    "change",
    [{"title": "Something else"}, {"run": 2}, {"picture": "pictures/t-0001-000000000000.svg"}],
)
def test_any_change_to_what_was_approved_voids_it(world: World, change: dict[str, object]) -> None:
    task_id = _approved(world)
    approval = approvals.current(world.places, task_id)
    task = world.queue().task(task_id)
    assert approval is not None and approvals.matches(approval, task)
    assert not approvals.matches(approval, dataclasses.replace(task, **change))
    assert not approvals.matches(dataclasses.replace(approval, model="claude-opus-5-5"), task)


def test_a_changed_title_sends_the_card_back_to_the_owner(world: World) -> None:
    task_id = _approved(world)
    assert proposals.read(world.places).approved == [task_id]
    tasks.set_facts(world.ctx(), task_id, {"title": "Sign-in loops on Safari"})
    assert world.queue().task(task_id).status == "proposed"
    assert approvals.current(world.places, task_id) is None
    found = proposals.read(world.places)
    assert (found.open[0], found.approved) == (task_id, [])


@pytest.mark.parametrize(("run", "status"), [(True, "building"), (False, "approved")], ids=["run-goes-on", "run-ended"])
def test_a_task_approved_again_while_its_run_goes_on_is_building_again(world: World, run: bool, status: str) -> None:
    task_id = _approved(world)
    tasks.move(world.ctx(), task_id, "building", "start", WARDEN)
    if run:
        world.session("s-impl", "implementer", f"impl-{task_id}", task_id, "/wt/x")
    tasks.set_facts(world.ctx(), task_id, {"title": "Sign-in loops on Safari"})

    owner.approve(world.owner(), task_id)

    assert world.queue().task(task_id).status == status


def test_approving_records_the_model_of_the_rung_and_frees_the_place(world: World) -> None:
    task_id = _approved(world)
    approval = approvals.current(world.places, task_id)
    assert approval is not None and approval.model == world.settings.model("implementer_small")[0]
    assert ("approved", {"task": task_id, "rung": 1, "model": approval.model}) in world.events
    assert ("proposal-slot-free", {"task": task_id}) in world.events
    assert proposals.read(world.places).open == [None]
