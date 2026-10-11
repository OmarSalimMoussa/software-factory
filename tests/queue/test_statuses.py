"""The one status table (plan 4.3, R1): a move off the table is refused, whoever asks."""

from __future__ import annotations

import dataclasses

import pytest
from queue_kit import World

from runtime.queue import statuses, tasks, turn
from runtime.queue.statuses import IMPL, ORCH, OWNER, WARDEN
from runtime.shared.models import Task
from runtime.shared.protocol import Refused

TASK = Task("t-0001", "T", ["r-2026-10-09-0001"], "G1", "major", ["ui"], "queued", "2026-10-09T12:00:00Z", "")


@pytest.mark.parametrize(
    ("source", "target", "event", "actor"),
    [
        ("queued", "approved", "approve", OWNER),  # only a proposal is approved
        ("proposed", "approved", "approve", ORCH),  # only the owner approves
        ("review", "building", "go", ORCH),  # only the owner says Go
        ("building", "qa", "qa-ready", ORCH),  # QA starts from the implementer's ready record
        ("qa", "turn", "turn", ORCH),  # no merge turn before qa-clean
        ("closed", "queued", "unpark", OWNER),  # closed is final
        ("deprioritized", "closed", "done", IMPL),
        ("merged", "on-staging", "on-staging", WARDEN),  # staging only after the check of the default branch
    ],
)
def test_a_move_off_the_table_is_refused(source: str, target: str, event: str, actor: str) -> None:
    with pytest.raises(Refused) as refused:
        statuses.move(dataclasses.replace(TASK, status=source), target, event, actor)
    assert refused.value.rule == "task-status"


def test_a_move_on_the_table_is_made() -> None:
    assert (
        statuses.move(dataclasses.replace(TASK, status="proposed"), "approved", "approve", OWNER).status == "approved"
    )


def test_the_orchestrator_names_the_event_only_when_the_table_holds_several(world: World) -> None:
    task = world.task()
    with pytest.raises(Refused, match="needs --event"):
        tasks.set_status(world.ctx(), task.id, "closed", "", "")
    closed = tasks.set_status(world.ctx(), task.id, "closed", "noise", "a duplicate")
    assert closed.status == "closed"
    assert world.queue().read_log(task.id)[-1]["event"] == "noise"


def test_deploys_move_checked_work_to_staging_then_prod(world: World) -> None:
    task = world.task(branch="impl-t-0001")
    world.queue().write_task(dataclasses.replace(task, status="turn", run=1))
    tasks.move(world.ctx(), task.id, "merged", "merged", "warden", "merged as abc1234", commit="abc1234")
    tasks.set_status(world.ctx(), task.id, "checked", "", "")
    included = {"5ta9e01", "9r0d001"}
    world.launcher.git = lambda argv: (0 if argv[-2:] == ["abc1234", argv[-1]] and argv[-1] in included else 1, "")
    assert tasks.advance_deployed(world.ctx(), "0ther00", None) == []
    assert tasks.advance_deployed(world.ctx(), "5ta9e01", None) == [f"{task.id} on-staging"]
    assert tasks.advance_deployed(world.ctx(), "5ta9e01", "9r0d001") == [f"{task.id} promoted"]
    assert world.queue().task(task.id).status == "promoted"


def test_one_task_holds_the_merge_turn_and_turn_next_picks_by_after_then_the_order(world: World) -> None:
    first, second, blocked = world.task(branch="impl-a"), world.task(branch="impl-b"), world.task(branch="impl-c")
    unlanded = world.task()
    for task, after in ((first, []), (second, []), (blocked, [unlanded.id])):
        world.queue().write_task(dataclasses.replace(task, status="qa-clean", run=1, after=after))
    world.launcher.git = lambda argv: (0, "c0ffee" * 6 + "abcd\n")

    assert turn.next_turn(world.ctx(), {}, None)["task"] == first.id
    assert turn.holder(world.places) == {"task": first.id, "branch": "impl-a", "base": "c0ffee" * 6 + "abcd"}
    with pytest.raises(Refused, match="holds the merge turn"):
        turn.next_turn(world.ctx(), {}, None)
    with pytest.raises(Refused, match="another task holds the merge turn"):
        tasks.set_status(world.ctx(), second.id, "turn", "", "")

    tasks.move(world.ctx(), first.id, "merged", "merged", "warden", "merged")
    assert turn.holder(world.places) is None
    assert turn.next_turn(world.ctx(), {}, None)["task"] == second.id  # the blocked one waits for its after task
