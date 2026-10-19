"""The one status table (plan 4.3, R1): a move off the table is refused, whoever asks."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from queue_kit import World

from runtime.queue import cards, documents, owner, proposals, statuses, tasks, turn, verbs
from runtime.queue.statuses import IMPL, ORCH, OWNER, WARDEN
from runtime.shared.models import BENCH_PENDING, DEPRIORITIZED, PAUSED, RETIRED, REVIVING, WAITING, Task
from runtime.shared.protocol import Refused
from runtime.warden import wardend
from runtime.warden.owner import reflections
from runtime.warden.sessions import backstop, lifecycle, permits, registry, respawn, revive

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
        ("turn", "merged", "merged", ORCH),  # the warden finds the merge itself
        ("merged", "checked", "checked", ORCH),  # and a saved check of it that passed
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


def test_one_task_holds_the_merge_turn_and_turn_next_picks_by_after_then_the_order(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second, blocked = world.task(branch="impl-a"), world.task(branch="impl-b"), world.task(branch="impl-c")
    unlanded = world.task()
    for task, after in ((first, []), (second, []), (blocked, [unlanded.id])):
        world.queue().write_task(dataclasses.replace(task, status="qa-clean", run=1, after=after))
        world.session(f"s-{task.id}", "implementer", task.branch, task.id)
    monkeypatch.setattr(backstop, "processes", lambda: {})
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


@pytest.mark.parametrize("state", [BENCH_PENDING, DEPRIORITIZED])
def test_the_merge_turn_skips_a_task_whose_implementer_is_benched_as_none_would_merge_it(
    world: World, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    benched, awake = world.task(branch="impl-a"), world.task(branch="impl-b")
    for task in (benched, awake):
        world.queue().write_task(dataclasses.replace(task, status="qa-clean", run=1))
        world.session(f"s-{task.id}", "implementer", task.branch, task.id)
    sessions = registry.read(world.places)
    sessions[f"s-{benched.id}"].state = state
    registry.write(world.places, sessions)
    monkeypatch.setattr(backstop, "processes", lambda: {})
    world.launcher.git = lambda argv: (0, "c0ffee" * 6 + "abcd\n")

    assert turn.next_turn(world.ctx(), {}, None)["task"] == awake.id


@pytest.mark.parametrize(
    ("state", "benched_for", "gives_back"),
    [(DEPRIORITIZED, "t-0009", True), (RETIRED, "", True), (DEPRIORITIZED, PAUSED, False), (WAITING, "", False)],
    ids=["benched-for-another-task", "ended", "paused-with-the-factory", "awake"],
)  # fmt: skip
def test_a_turn_whose_implementer_was_benched_or_ended_goes_back_so_the_next_task_merges(
    world: World, monkeypatch: pytest.MonkeyPatch, state: str, benched_for: str, gives_back: bool
) -> None:
    holding = world.task(branch="impl-a")
    world.queue().write_task(dataclasses.replace(holding, status="qa-clean", run=1))
    world.session("s-impl", "implementer", "impl-a", holding.id)
    monkeypatch.setattr(backstop, "processes", lambda: {})
    world.launcher.git = lambda argv: (0, "c0ffee" * 6 + "abcd\n")  # no merge on the default branch yet
    turn.next_turn(world.ctx(), {}, None)
    sessions = registry.read(world.places)
    sessions["s-impl"].state, sessions["s-impl"].benched_for = state, benched_for
    registry.write(world.places, sessions)

    turn.find_merge(world.ctx())

    assert world.queue().task(holding.id).status == ("qa-clean" if gives_back else "turn")
    assert (turn.holder(world.places) is None) == gives_back


def test_an_approved_spec_gives_its_task_the_slice_count_it_lists(world: World) -> None:
    task = world.task(slices=1)
    world.queue().write_task(dataclasses.replace(task, status="designing", run=3))
    with pytest.raises(Refused, match="--slices"):
        documents.save_spec(world.ctx("designer", "s-design"), task.id, b"# A spec\n\nThe plan.\n", None)
    documents.save_spec(world.ctx("designer", "s-design"), task.id, b"# A spec\n\nFour slices.\n", 4)
    assert world.queue().task(task.id).slices == 1  # an estimate until the spec is approved

    tasks.set_status(world.ctx(), task.id, "building", "spec-approved", "")
    assert world.queue().task(task.id).slices == 4


@pytest.mark.parametrize(
    ("implementer", "run", "status", "back"),
    [("working", 1, "merged", "building"), ("deprioritized", 1, "merged", "building"),
     ("retired", 1, "on-staging", "approved"), ("retired", 3, "on-staging", "building")],
    ids=["its-implementer-is-told", "its-benched-implementer-is-told-at-its-revive",
         "a-new-implementer-starts-told", "a-designed-builds-new-implementer-starts-told"],
)  # fmt: skip
def test_a_reverted_task_goes_back_to_its_implementer_or_once_that_ended_to_a_new_one(
    world: World, monkeypatch: pytest.MonkeyPatch, implementer: str, run: int, status: str, back: str
) -> None:
    task = world.task(branch="impl-t-0001")
    world.queue().write_task(dataclasses.replace(task, status=status, run=run))
    world.session("s-impl", "implementer", "impl-t-0001", task.id)
    sessions = registry.read(world.places)
    sessions["s-impl"].state = implementer  # retired: its task passed its check, so its implementer finished
    registry.write(world.places, sessions)
    monkeypatch.setattr(backstop, "processes", lambda: {"s-impl": None} if implementer == "working" else {})

    with pytest.raises(Refused, match="--note"):
        tasks.set_status(world.ctx(), task.id, "building", "", "")
    moved = tasks.set_status(world.ctx(), task.id, "building", "", "staging regressed; reverted as 0abc123")

    told = "Your work was taken back off master: staging regressed; reverted as 0abc123"
    held = lifecycle.held_path(world.ctx(), task.id)
    assert moved.status == back
    if implementer == "working":
        assert [(sid, told in text) for sid, text in world.launcher.messages] == [("s-impl", True)]
        assert not held.exists()
    else:  # its next start or revive carries the word, once
        assert told in held.read_text() and world.launcher.messages == []
    if implementer == "retired":  # the scheduler starts it as it starts any approved run or designed build
        waiting = proposals.designed_builds(world.places) if run >= 3 else proposals.read(world.places).approved
        assert waiting == [task.id]


BLOCK = "qa-ux cannot reach the review: the slots have no OPENROUTER_API_KEY; reverted as 0abc123"


def _blocked(world: World, monkeypatch: pytest.MonkeyPatch, status: str, implementer: str) -> Task:
    task = world.task(branch="impl-t-0001")
    world.queue().write_task(dataclasses.replace(task, status=status, run=1))
    world.session("s-impl", "implementer", "impl-t-0001", task.id)
    sessions = registry.read(world.places)
    sessions["s-impl"].state = implementer
    registry.write(world.places, sessions)
    monkeypatch.setattr(backstop, "processes", lambda: {})
    with pytest.raises(Refused, match="--note"):
        tasks.set_status(world.ctx(), task.id, "blocked", "", " ")
    with pytest.raises(Refused, match="--event blocked moves"):
        tasks.set_status(world.ctx(), task.id, "building", "blocked", BLOCK)
    tasks.set_status(world.ctx(), task.id, "blocked", "", BLOCK)
    return task


@pytest.mark.parametrize(
    ("status", "implementer", "benched", "off_master"),
    [("merged", DEPRIORITIZED, DEPRIORITIZED, True), ("qa", "working", BENCH_PENDING, False)],
    ids=["merged-then-reverted", "in-qa"],
)
def test_a_blocked_task_waits_benched_for_the_owners_fixed_then_goes_back_to_its_implementer(
    world: World, monkeypatch: pytest.MonkeyPatch, status: str, implementer: str, benched: str, off_master: bool
) -> None:
    task = _blocked(world, monkeypatch, status, implementer)

    assert (world.queue().task(task.id).status, registry.read(world.places)["s-impl"].state) == ("blocked", benched)
    [card] = cards.read(world.places)
    assert (card.kind, card.task, card.ask, card.answered) == ("blocked", task.id, BLOCK, "")
    assert [e["text"] for e in reflections.entries(world.places)] == [
        f"{task.id} is blocked until the owner fixes what stopped it: {BLOCK}"
    ]
    assert not lifecycle.held_path(world.ctx(), task.id).exists()
    with pytest.raises(Refused):
        tasks.set_status(world.ctx(), task.id, "building", "", "the orchestrator unblocks it")

    owner.fixed(world.owner(), card.id)

    told = lifecycle.held_path(world.ctx(), task.id).read_text()
    assert BLOCK in told and ("taken back off master" in told) == off_master
    assert world.queue().task(task.id).status == "building"
    assert [c.answered for c in cards.read(world.places)] == ["building"]
    assert world.events[-1] == ("unblocked", {"task": task.id, "card": card.id})
    with pytest.raises(Refused):
        owner.fixed(world.owner(), card.id)


def test_a_blocked_task_the_owner_drops_takes_its_card_with_it(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    task = _blocked(world, monkeypatch, "merged", DEPRIORITIZED)

    owner.drop(world.owner(), task.id, "the outside site is gone for good")

    assert [c.answered for c in cards.read(world.places)] == ["closed"]


HOLD = "qa-regression needs the practice page the other task serves; reverted as 0abc123"


@pytest.mark.parametrize(
    ("status", "implementer", "benched", "off_master"),
    [("merged", DEPRIORITIZED, DEPRIORITIZED, True), ("qa", "working", BENCH_PENDING, False)],
    ids=["merged-then-reverted", "in-qa"],
)
def test_a_task_on_hold_waits_benched_for_the_task_it_names_then_the_warden_sends_it_back(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: str, implementer: str, benched: str,
    off_master: bool,
) -> None:  # fmt: skip
    release = dict(wardend.passes(world.places, [], tmp_path))["holds"]
    task, maker = world.task(branch="impl-t-0001"), dataclasses.replace(world.task(), run=1)
    world.queue().write_task(dataclasses.replace(task, status=status, run=1))
    world.session("s-impl", "implementer", "impl-t-0001", task.id)
    sessions = registry.read(world.places)
    sessions["s-impl"].state = implementer
    registry.write(world.places, sessions)
    monkeypatch.setattr(backstop, "processes", lambda: {})
    hold = {"task": task.id, "to": "on-hold", "note": HOLD}
    for after, said in (("", "--after, the task"), (task.id, "--after, the task"), ("t-0099", "no task t-0099")):
        with pytest.raises(Refused, match=said):
            verbs.task_status(world.ctx(), {**hold, "after": after}, None)
    with pytest.raises(Refused, match="goes only with on-hold"):
        verbs.task_status(world.ctx(), {**hold, "to": "blocked", "after": maker.id}, None)

    verbs.task_status(world.ctx(), {**hold, "after": maker.id}, None)

    held = world.queue().task(task.id)
    assert (held.status, held.after, registry.read(world.places)["s-impl"].state) == ("on-hold", [maker.id], benched)
    assert cards.read(world.places) == [] and reflections.entries(world.places) == []
    for not_yet in ("queued", "building", "merged"):  # a merge may still be reverted: its check has not passed
        world.queue().write_task(dataclasses.replace(maker, status=not_yet))
        release(world.ctx())
        assert world.queue().task(task.id).status == "on-hold"
    world.queue().write_task(dataclasses.replace(maker, status="checked"))

    release(world.ctx())

    told = lifecycle.held_path(world.ctx(), task.id).read_text()
    assert HOLD in told and f"{maker.id}, which it waited for, passed its check: carry on." in told
    assert ("taken back off master" in told) == off_master
    assert world.queue().task(task.id).status == "building"
    assert world.queue().read_log(task.id)[-1]["event"] == "released"
    with pytest.raises(Refused, match="passed its check already"):
        verbs.task_status(world.ctx(), {**hold, "after": maker.id}, None)


@pytest.mark.parametrize("gone", ["closed", "parked"])
def test_a_task_on_hold_goes_back_told_why_once_the_task_it_waits_for_is_closed_or_parked(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gone: str
) -> None:
    release = dict(wardend.passes(world.places, [], tmp_path))["holds"]
    task, maker = world.task(), dataclasses.replace(world.task(), run=1)
    world.queue().write_task(dataclasses.replace(task, status="qa", run=1))
    monkeypatch.setattr(backstop, "processes", lambda: {})
    verbs.task_status(world.ctx(), {"task": task.id, "to": "on-hold", "note": HOLD, "after": maker.id}, None)
    world.queue().write_task(dataclasses.replace(maker, status=gone))

    release(world.ctx())

    told = lifecycle.held_path(world.ctx(), task.id).read_text()
    assert f"{maker.id}, which it waited for, was {gone} before its check passed: find another way" in told
    assert world.queue().task(task.id).status == "approved"  # no implementer is left: a new one starts


def test_the_orchestrator_releases_its_own_hold_and_the_task_no_longer_waits_to_merge_after_the_other(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    task, maker = world.task(), dataclasses.replace(world.task(), run=1)
    world.queue().write_task(dataclasses.replace(task, status="qa", run=1))
    monkeypatch.setattr(backstop, "processes", lambda: {})
    verbs.task_status(world.ctx(), {"task": task.id, "to": "on-hold", "note": HOLD, "after": maker.id}, None)
    with pytest.raises(Refused, match="says why"):
        verbs.task_status(world.ctx(), {"task": task.id, "to": "building"}, None)

    verbs.task_status(world.ctx(), {"task": task.id, "to": "building", "note": "its walk never fits the desktop"}, None)

    released = world.queue().task(task.id)
    assert (released.status, released.after) == ("approved", [])  # no implementer is left: a new one starts
    assert world.queue().read_log(task.id)[-1]["event"] == "released"
    assert "let it go on without what it waited for" in lifecycle.held_path(world.ctx(), task.id).read_text()


def test_a_hold_on_a_task_that_makes_nothing_or_waits_back_on_it_is_refused(world: World) -> None:
    task, other, third = world.task(), *(dataclasses.replace(world.task(), run=1) for _ in range(2))
    world.queue().write_task(dataclasses.replace(task, status="qa", run=1))
    world.queue().write_task(dataclasses.replace(third, status="building", after=[task.id]))
    for status, after, said in (
        ("parked", [], f"{other.id} is parked, so nothing makes what {task.id} lacks"),
        ("closed", [], "is closed"),
        ("building", [third.id], f"{other.id} waits, through its after tasks, for {task.id}"),
    ):
        world.queue().write_task(dataclasses.replace(other, status=status, after=after))
        with pytest.raises(Refused, match=said):
            verbs.task_status(world.ctx(), {"task": task.id, "to": "on-hold", "note": HOLD, "after": other.id}, None)


def test_a_revert_said_while_its_implementer_comes_back_follows_it_once_it_is_back(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = world.task(branch="impl-t-0001")
    world.queue().write_task(dataclasses.replace(task, status="merged", run=1))
    world.session("s-impl", "implementer", "impl-t-0001", task.id)
    sessions = registry.read(world.places)
    sessions["s-impl"].state = REVIVING  # its revive is under way, outside the lock
    registry.write(world.places, sessions)
    monkeypatch.setattr(backstop, "processes", lambda: {})
    monkeypatch.setattr(permits, "claim_slot", lambda ctx, s: None)
    resumed: list[str] = []

    def resume(ctx: object, s: object, message: str) -> str:
        resumed.append(message)
        if len(resumed) == 1:  # the orchestrator's revert lands while the revive runs
            tasks.set_status(world.ctx(), task.id, "building", "", "staging regressed; reverted as 0abc123")
        return "s-impl"

    monkeypatch.setattr(respawn, "resume", resume)

    assert revive.bring_back(world.ctx(), "s-impl", "warden") == ""

    assert len(resumed) == 2 and "Your work was taken back off master" in resumed[1]
    assert not lifecycle.held_path(world.ctx(), task.id).exists()
