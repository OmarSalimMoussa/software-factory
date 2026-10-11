"""The one status table (plan 4.3, R1): a move off the table is refused, whoever asks."""

from __future__ import annotations

import dataclasses

import pytest
from queue_kit import World

from runtime.queue import documents, proposals, records, statuses, tasks, turn, verbs
from runtime.queue.statuses import IMPL, ORCH, OWNER, WARDEN
from runtime.shared import store
from runtime.shared.models import RETIRED, Task
from runtime.shared.protocol import Refused
from runtime.warden import backstop, lifecycle, registry

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


def test_task_statuses_follow_the_builds_forward_and_back(world: World) -> None:
    task = world.task(branch="impl-t-0001")
    world.queue().write_task(dataclasses.replace(task, status="turn", run=1))
    tasks.move(world.ctx(), task.id, "merged", "merged", "warden", "merged as abc1234", commit="abc1234")
    tasks.move(world.ctx(), task.id, "checked", "checked", "warden", "the check passed")
    included = {"5ta9e01", "9r0d001"}

    def includes(build: str, merge: str) -> bool | None:
        return None if build == "unkn0wn" else (merge == "abc1234" and build in included)

    def advance(staging: str | None, prod: str | None) -> list[str]:
        return tasks.advance_deployed(world.ctx(), staging, prod, includes)

    assert advance("0ther00", None) == []
    assert advance("unkn0wn", None) == []  # a build git does not know moves nothing, and stalls nothing
    assert advance("5ta9e01", None) == [f"{task.id} on-staging"]
    assert advance("5ta9e01", "9r0d001") == [f"{task.id} promoted"]
    assert advance("5ta9e01", "01d0001") == [f"{task.id} on-staging"]  # prod pushed back
    assert advance("01d0002", "01d0001") == [f"{task.id} checked"]  # and staging moved off it
    assert world.queue().task(task.id).status == "checked"


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


def test_an_approved_spec_gives_its_task_the_slice_count_it_lists(world: World) -> None:
    task = world.task(slices=1)
    world.queue().write_task(dataclasses.replace(task, status="designing", run=3))
    with pytest.raises(Refused, match="--slices"):
        documents.save_spec(world.ctx("designer", "s-design"), task.id, b"# A spec\n\nThe plan.\n", None)
    documents.save_spec(world.ctx("designer", "s-design"), task.id, b"# A spec\n\nFour slices.\n", 4)
    assert world.queue().task(task.id).slices == 1  # an estimate until the spec is approved

    tasks.set_status(world.ctx(), task.id, "building", "spec-approved", "")
    assert world.queue().task(task.id).slices == 4


@pytest.mark.parametrize("live", [True, False], ids=["its-implementer-is-told", "a-new-implementer-starts-told"])
def test_a_reverted_task_goes_back_to_its_implementer_or_once_that_ended_to_a_new_one(
    world: World, monkeypatch: pytest.MonkeyPatch, live: bool
) -> None:
    task = world.task(branch="impl-t-0001")
    world.queue().write_task(dataclasses.replace(task, status="merged" if live else "on-staging", run=1))
    world.session("s-impl", "implementer", "impl-t-0001", task.id)
    if live:
        monkeypatch.setattr(backstop, "processes", lambda: {"s-impl": None})
    else:  # its task passed its check, so its implementer finished
        sessions = registry.read(world.places)
        sessions["s-impl"].state = RETIRED
        registry.write(world.places, sessions)

    with pytest.raises(Refused, match="--note"):
        tasks.set_status(world.ctx(), task.id, "building", "", "")
    back = tasks.set_status(world.ctx(), task.id, "building", "", "staging regressed; reverted as 0abc123")

    told = "Your work was taken back off master: staging regressed; reverted as 0abc123"
    assert back.status == ("building" if live else "approved")
    if live:
        assert [(sid, told in text) for sid, text in world.launcher.messages] == [("s-impl", True)]
    else:  # the scheduler starts the approved run with its brief, which now says why first
        assert told in lifecycle.brief_path(world.ctx(), task.id).read_text()
        assert proposals.read(world.places).approved == [task.id] and world.launcher.messages == []


def test_a_check_named_by_a_short_sha_checks_the_merge_it_ran_on(world: World) -> None:
    task = world.task(branch="impl-t-0001")
    world.queue().write_task(dataclasses.replace(task, status="merged", run=1))
    merge = "abc1234" + "0" * 33
    store.write_json(turn.merges_path(world.places), {merge: task.id})
    world.launcher.git = lambda argv: (0, f"{merge}\n") if argv[-1] == "abc1234^{commit}" else (1, "")
    report = (f"# Check\n\n## Scenarios\n\n{records.CHECK_TABLE}\n| --- | --- | --- | --- |\n"
              "| sign-in | pass | 3 | shot.png |\n").encode()  # fmt: skip

    with pytest.raises(Refused, match="git rev-parse failed"):
        verbs.check_save(world.ctx(), {"commit": "fedcba9"}, report)
    answer = verbs.check_save(world.ctx(), {"commit": "abc1234"}, report)
    assert answer["checked"] == task.id and world.queue().task(task.id).status == "checked"
    assert [c.commit for c in world.queue().checks()[0]] == [merge]
