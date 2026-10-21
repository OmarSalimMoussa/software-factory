from __future__ import annotations

import logging
from typing import Any

import pytest
from lifecycle_fakes import World, at_once

from runtime.shared import store
from runtime.shared.models import BENCH_PENDING, DEPRIORITIZED, RETIRED, REVIVING, STOPPED, WAITING, WORKING
from runtime.shared.protocol import Refused
from runtime.slots import leases
from runtime.warden import checks, schedule
from runtime.warden.owner import ops
from runtime.warden.sessions import crashwatch, lifecycle, older_skills, permits, revive


def rule(call: Any, *args: Any) -> str:
    with pytest.raises(Refused) as refused:
        call(*args)
    return str(refused.value.rule)


@pytest.mark.parametrize("state", [DEPRIORITIZED, BENCH_PENDING, REVIVING])
def test_only_the_owner_retires_a_session_that_is_out_of_its_place(world: World, state: str) -> None:
    world.queue.add("t-0001", status="deprioritized")
    world.seat("5e550001", task="t-0001", state=state)
    assert rule(lifecycle.stop, world.ctx(), {"session": "5e550001"}, None) == "deprioritized"
    assert world.session("5e550001").state == state
    lifecycle.stop(world.owner(), {"session": "5e550001"}, None)
    retired = world.session("5e550001")
    assert (retired.state, retired.retired, retired.retired_by) == (RETIRED, STOPPED, "owner")
    assert world.queue.tasks["t-0001"].status == "proposed"


@pytest.mark.parametrize("ref", ["5e550001", "implementer-5e550001"])
def test_a_retired_session_is_never_revived_or_resumed(world: World, ref: str) -> None:
    world.seat("5e550001", task="t-0001", state=RETIRED, retired=STOPPED)
    assert rule(lifecycle.resume, world.ctx(), {"session": ref, "message": "carry on"}, None) == "retired"
    assert rule(revive.revive_session, world.ctx(), "5e550001", "warden") == "retired"
    assert world.launcher.resumed == []


def _approved(world: World, *order: str) -> None:
    store.write_json(world.places.proposals, {"open": [], "approved": list(order)})


def test_a_bench_makes_room_only_for_a_task_that_outranks_the_one_it_replaces(world: World) -> None:
    world.seat("0c0c0c0c", role="orchestrator", family="orch")  # runs start in a working factory
    world.queue.add("t-0001", goal_item="G1", status="building", severity="severe", created="2026-10-02T00:00:00Z")
    world.seat("5e550001", task="t-0001")
    world.approve("t-0002", goal_item="G2")
    _approved(world, "t-0002")
    schedule.run_pass(world.ctx())
    assert world.session("5e550001").state == WORKING  # a severe run outranks any approved one
    world.approve("t-0003", severity="severe", created="2026-09-01T00:00:00Z")  # an older severe one outranks it
    _approved(world, "t-0002", "t-0003")
    schedule.run_pass(world.ctx())
    assert world.session("5e550001").state == BENCH_PENDING


@pytest.mark.parametrize(
    ("new", "since", "benched"),
    [
        ({"goal_item": "G2"}, "2026-10-09T11:00:00Z", False),
        ({"goal_item": "G1"}, "2026-10-09T11:00:00Z", True),
        ({"goal_item": "G1"}, "2026-10-09T11:55:00Z", False),
        ({"goal_item": "G2", "severity": "severe"}, "2026-10-09T11:55:00Z", True),
    ],
    ids=["approval-alone", "higher-goal", "too-soon", "severe-at-once"],
)
def test_only_severe_work_or_a_higher_goal_item_benches_a_run_and_not_in_its_first_minutes(
    world: World, new: dict[str, str], since: str, benched: bool
) -> None:
    world.seat("0c0c0c0c", role="orchestrator", family="orch")
    world.queue.add("t-0001", goal_item="G2", status="building")
    world.seat("5e550001", task="t-0001", since=since)
    world.approve("t-0002", **new)
    _approved(world, "t-0002")

    schedule.run_pass(world.ctx())

    # An approved run outranked every running one, and lost that rank once it started: each start benched the last.
    assert (world.session("5e550001").state == BENCH_PENDING) is benched


@pytest.mark.parametrize("turn", [False, True], ids=["waiting-first", "never-a-merge-turn"])
def test_a_bench_takes_a_waiting_session_first_and_never_a_merge_turn(world: World, turn: bool) -> None:
    world.seat("0c0c0c0c", role="orchestrator", family="orch")  # runs start in a working factory
    world.cap(2)
    world.queue.add("t-0001", goal_item="G2", status="building")
    world.queue.add("t-0002", goal_item="G2", status="building")
    world.seat("3c3c3c3c", task="t-0001")
    world.seat("4d4d4d4d", task="t-0002", state=WAITING, merge_turn=turn)
    world.approve("t-0003", severity="severe")
    _approved(world, "t-0003")
    schedule.run_pass(world.ctx())
    benched = "3c3c3c3c" if turn else "4d4d4d4d"
    assert [s.id for s in lifecycle.read(world.ctx()).values() if s.state == BENCH_PENDING] == [benched]


def test_a_benched_session_settles_after_its_grace_and_revives_with_its_id(world: World) -> None:
    world.seat("0c0c0c0c", role="orchestrator", family="orch")  # runs start in a working factory
    world.run("0c0c0c0c")
    world.queue.add("t-0001", goal_item="G2", status="building")
    benched_name = world.seat("5e550001", task="t-0001").name
    leases.claim(world.ctx(), "someone-else", "/w/x")  # slot 1
    assert leases.claim(world.ctx(), benched_name, "/w/b") == 2
    world.run("5e550001")
    world.approve("t-0002", severity="severe")
    _approved(world, "t-0002")
    schedule.run_pass(world.ctx())
    crashwatch.SessionsWatch(start=at_once).run_pass(world.ctx())
    assert world.session("5e550001").state == BENCH_PENDING and world.launcher.stopped == []
    world.later(world.settings.get_int("watch.bench_grace_seconds"))
    crashwatch.SessionsWatch(start=at_once).run_pass(world.ctx())
    benched = world.session("5e550001")
    assert benched.state == DEPRIORITIZED and leases.held_by(world.places, world.settings, benched_name) is None
    assert world.launcher.stopped == ["5e550001"] and world.launcher.stacks == [(2, ["empty"]), (2, [leases.SWEEP])]
    assert world.queue.tasks["t-0001"].status == "deprioritized"
    store.write_text(lifecycle.held_path(world.ctx(), "t-0001"), "Your work was taken back off master.\n")
    revive.revive_session(world.ctx(), "5e550001", "warden")
    [(_, said)] = world.launcher.messages
    assert "Your work was taken back off master." in said  # a word held for its run
    assert not lifecycle.held_path(world.ctx(), "t-0001").exists()
    back = world.session("5e550001")
    lease = leases.held_by(world.places, world.settings, benched_name)
    assert (back.state, lease and lease.slot, world.launcher.resumed) == (WORKING, 2, ["5e550001"])
    assert world.queue.tasks["t-0001"].status == "building"


def test_pause_benches_every_session_and_resume_brings_them_back_in_order_each_with_todays_definitions(
    world: World,
) -> None:
    world.cap(2)
    world.queue.add("t-0001", goal_item="G1", status="building")
    world.queue.add("t-0002", goal_item="G2", status="building")
    world.seat("2b2b2b2b", task="t-0002", definitions="older")
    world.seat("0c0c0c0c", role="orchestrator", family="orch")
    world.seat("1a1a1a1a", task="t-0001", state=WAITING)
    for sid in ("2b2b2b2b", "0c0c0c0c", "1a1a1a1a"):
        world.run(sid)
    op_id = revive.owner_pause(world.owner(), {}, None)["op"]
    assert {world.session(s).state for s in ("2b2b2b2b", "0c0c0c0c", "1a1a1a1a")} == {BENCH_PENDING}
    assert ops.get(world.owner(), {"id": op_id}, None)["op"]["state"] == ops.RUNNING
    assert rule(revive.owner_resume, world.owner(), {}, None) == "settling"
    world.later(world.settings.get_int("watch.bench_grace_seconds"))
    crashwatch.SessionsWatch(start=at_once).run_pass(world.ctx())
    assert {world.session(s).state for s in ("2b2b2b2b", "0c0c0c0c", "1a1a1a1a")} == {DEPRIORITIZED}
    assert ops.get(world.owner(), {"id": op_id}, None)["op"]["state"] == ops.DONE
    back = revive.owner_resume(world.owner(), {}, None)["op"]
    ops.join(back)
    assert ops.get(world.owner(), {"id": back}, None)["op"]["state"] == ops.DONE
    assert world.launcher.resumed == ["0c0c0c0c", "1a1a1a1a"]
    assert [request["resume"] for _, request in world.launcher.started] == ["2b2b2b2b"]
    assert {world.session(s).state for s in ("copy-2b2b2b2b", "0c0c0c0c", "1a1a1a1a")} == {WORKING}
    assert [kind for kind, _ in world.events] == ["paused", "benched", "benched", "benched", "resumed"]


def test_resume_leaves_benched_a_session_whose_task_waits_for_its_merge_check(
    world: World,
) -> None:
    world.queue.add("t-0001", goal_item="G1", status="merged")
    world.seat("0c0c0c0c", role="orchestrator", family="orch")
    world.seat("1a1a1a1a", task="t-0001", state=WAITING)
    for sid in ("0c0c0c0c", "1a1a1a1a"):
        world.run(sid)
    revive.owner_pause(world.owner(), {}, None)
    world.later(world.settings.get_int("watch.bench_grace_seconds"))
    crashwatch.SessionsWatch(start=at_once).run_pass(world.ctx())

    answer = revive.owner_resume(world.owner(), {}, None)
    ops.join(answer["op"])

    assert (answer["resuming"], answer["waiting"]) == (
        [world.session("0c0c0c0c").name],
        [world.session("1a1a1a1a").name],
    )
    assert (world.launcher.resumed, world.session("1a1a1a1a").state) == (["0c0c0c0c"], DEPRIORITIZED)


def test_a_paused_session_whose_task_moves_past_its_part_on_its_way_to_the_bench_ends_and_the_pause_completes(
    world: World,
) -> None:
    world.queue.add("t-0001", status="review", run=3)
    world.seat("0c0c0c0c", role="orchestrator", family="orch")
    world.seat("de510001", role="designer", task="t-0001", state=WAITING)
    for sid in ("0c0c0c0c", "de510001"):
        world.run(sid)
    op_id = revive.owner_pause(world.owner(), {}, None)["op"]
    world.queue.move(world.ctx(), "t-0001", "building", "go", "owner")  # the owner's Go, within the bench's grace

    crashwatch.SessionsWatch(start=at_once).run_pass(world.ctx())
    assert (world.session("de510001").state, world.session("de510001").retired) == (RETIRED, "finished")
    world.later(world.settings.get_int("watch.bench_grace_seconds"))
    crashwatch.SessionsWatch(start=at_once).run_pass(world.ctx())

    assert ops.get(world.owner(), {"id": op_id}, None)["op"]["state"] == ops.DONE


def test_a_session_alive_when_a_new_factory_is_installed_is_told_once_to_load_its_skills_again(world: World) -> None:
    world.seat("0c0c0c0c", role="orchestrator", family="orch", state=DEPRIORITIZED)
    # The new tree is in place, and the warden starts on it, before the install writes its record.
    store.write_text(world.places.install / "COMMIT", "a" * 40 + "\n")
    older_skills.note_install(world.places)
    world.seat("1a1a1a1a", role="orchestrator", family="orch", state=DEPRIORITIZED)
    older_skills.note_install(world.places)  # the same install, noted at a second start: the newer session is not

    revive.revive_session(world.ctx(), "0c0c0c0c", "warden")
    revive.revive_session(world.ctx(), "1a1a1a1a", "warden")
    world.seat("0c0c0c0c", role="orchestrator", family="orch", state=DEPRIORITIZED)
    revive.revive_session(world.ctx(), "0c0c0c0c", "warden")

    told = [(sid, older_skills.RELOAD in said) for sid, said in world.launcher.messages]
    assert told == [("0c0c0c0c", True), ("1a1a1a1a", False), ("0c0c0c0c", False)]


def test_a_check_that_ends_after_its_session_was_benched_is_told_once_as_it_comes_back(world: World) -> None:
    world.seat("0c0c0c0c", role="orchestrator", family="orch")

    def benched_midway(checkout: str, args: list[str]) -> tuple[int, str]:
        world.seat("0c0c0c0c", role="orchestrator", family="orch", state=DEPRIORITIZED)
        return 1, "factory check: 1 of 29 failed\n"

    world.launcher.check = benched_midway
    checks.check(world.ctx(session="0c0c0c0c"), {"only": ["comment-budgets"]}, None)
    revive.revive_session(world.ctx(), "0c0c0c0c", "warden")
    world.seat("0c0c0c0c", role="orchestrator", family="orch", state=DEPRIORITIZED)
    revive.revive_session(world.ctx(), "0c0c0c0c", "warden")

    told = [said.count("ended after you were benched: it failed: run it again") for _, said in world.launcher.messages]
    assert told == [1, 0]


@pytest.mark.parametrize(("behind", "told", "logged"), [(3, True, False), (0, False, False), (None, False, True)])
def test_a_revived_implementer_is_told_how_far_the_default_branch_moved(
    world: World, caplog: pytest.LogCaptureFixture, behind: int | None, told: bool, logged: bool
) -> None:
    world.queue.add("t-0001", status="deprioritized")
    world.seat("5e550001", task="t-0001", state=DEPRIORITIZED)
    world.launcher.behind = behind

    with caplog.at_level(logging.WARNING):
        revive.revive_session(world.ctx(), "5e550001", "warden")

    [(_, said)] = world.launcher.messages
    assert ("The default branch has 3 commits your branch lacks: `git rebase " in said) == told
    assert any("was not compared with" in r.getMessage() for r in caplog.records) == logged


@pytest.mark.parametrize(("status", "slot", "told"), [
    ("deprioritized", 1, "Slot 1 is yours again: rebuild it"),
    ("qa-clean", None, permits.AWAITS_TURN),
])  # fmt: skip
def test_a_revived_implementer_rebuilds_its_slot_unless_its_task_waits_qa_clean_for_its_merge_turn(
    world: World, status: str, slot: int | None, told: str
) -> None:
    world.queue.add("t-0001", status=status)
    world.seat("5e550001", task="t-0001", state=DEPRIORITIZED)
    world.launcher.behind = 3

    revive.revive_session(world.ctx(), "5e550001", "warden")

    [(_, said)] = world.launcher.messages
    lease = leases.held_by(world.places, world.settings, world.session("5e550001").name)
    assert (told in said, "git rebase" in said, lease and lease.slot) == (True, slot is not None, slot)


def test_a_stopped_run_that_never_was_a_proposal_goes_back_to_the_queue(world: World) -> None:
    world.queue.add("t-0001", status="building", why="", picture="")
    world.seat("5e550001", task="t-0001")
    lifecycle.stop(world.ctx(), {"session": "5e550001"}, None)
    assert world.queue.tasks["t-0001"].status == "queued"
