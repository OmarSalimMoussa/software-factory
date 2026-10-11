from __future__ import annotations

from typing import Any

import pytest
from lifecycle_fakes import World, at_once

from runtime.shared import store
from runtime.shared.models import BENCH_PENDING, DEPRIORITIZED, RETIRED, REVIVING, STOPPED, WAITING, WORKING
from runtime.shared.protocol import Refused
from runtime.slots import leases
from runtime.warden import crashwatch, lifecycle, ops, schedule


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
    lifecycle.owner_stop(world.owner(), {"session": "5e550001"}, None)
    retired = world.session("5e550001")
    assert (retired.state, retired.retired, retired.retired_by) == (RETIRED, STOPPED, "owner")
    assert world.queue.tasks["t-0001"].status == "proposed"


@pytest.mark.parametrize("ref", ["5e550001", "implementer-5e550001"])
def test_a_retired_session_is_never_revived_or_resumed(world: World, ref: str) -> None:
    world.seat("5e550001", task="t-0001", state=RETIRED, retired=STOPPED)
    assert rule(lifecycle.resume, world.ctx(), {"session": ref, "message": "carry on"}, None) == "retired"
    assert rule(lifecycle.revive_session, world.ctx(), "5e550001", "warden") == "retired"
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
    lifecycle.revive_session(world.ctx(), "5e550001", "warden")
    resumed = next(argv for argv in world.launcher.commands if argv[:2] == ["claude", "--resume"])
    assert any("Your work was taken back off master." in word for word in resumed)  # a word held for its run
    assert not lifecycle.held_path(world.ctx(), "t-0001").exists()
    back = world.session("5e550001")
    lease = leases.held_by(world.places, world.settings, benched_name)
    assert (back.state, lease and lease.slot, world.launcher.resumed) == (WORKING, 2, ["5e550001"])
    assert world.queue.tasks["t-0001"].status == "building"


def test_pause_benches_every_session_and_resume_brings_them_back_in_order(world: World) -> None:
    world.cap(2)
    world.queue.add("t-0001", goal_item="G1", status="building")
    world.queue.add("t-0002", goal_item="G2", status="building")
    world.seat("2b2b2b2b", task="t-0002")
    world.seat("0c0c0c0c", role="orchestrator", family="orch")
    world.seat("1a1a1a1a", task="t-0001", state=WAITING)
    for sid in ("2b2b2b2b", "0c0c0c0c", "1a1a1a1a"):
        world.run(sid)
    op_id = lifecycle.owner_pause(world.owner(), {}, None)["op"]
    assert {world.session(s).state for s in ("2b2b2b2b", "0c0c0c0c", "1a1a1a1a")} == {BENCH_PENDING}
    assert ops.get(world.owner(), {"id": op_id}, None)["op"]["state"] == ops.RUNNING
    assert rule(lifecycle.owner_resume, world.owner(), {}, None) == "settling"
    world.later(world.settings.get_int("watch.bench_grace_seconds"))
    crashwatch.SessionsWatch(start=at_once).run_pass(world.ctx())
    assert {world.session(s).state for s in ("2b2b2b2b", "0c0c0c0c", "1a1a1a1a")} == {DEPRIORITIZED}
    assert ops.get(world.owner(), {"id": op_id}, None)["op"]["state"] == ops.DONE
    lifecycle.owner_resume(world.owner(), {}, None)
    assert world.launcher.resumed == ["0c0c0c0c", "1a1a1a1a", "2b2b2b2b"]
    assert {world.session(s).state for s in ("2b2b2b2b", "0c0c0c0c", "1a1a1a1a")} == {WORKING}
    assert [kind for kind, _ in world.events] == ["paused", "benched", "benched", "benched", "resumed"]


def test_a_stopped_run_that_never_was_a_proposal_goes_back_to_the_queue(world: World) -> None:
    world.queue.add("t-0001", status="building", why="", picture="")
    world.seat("5e550001", task="t-0001")
    lifecycle.stop(world.ctx(), {"session": "5e550001"}, None)
    assert world.queue.tasks["t-0001"].status == "queued"
