from __future__ import annotations

from typing import Any

import pytest
from lifecycle_fakes import World

from runtime.shared.models import BENCH_PENDING, DEPRIORITIZED, RETIRED, REVIVING, STOPPED, WAITING, WORKING
from runtime.shared.protocol import Refused
from runtime.slots import leases
from runtime.warden import crashwatch, lifecycle, ops


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


@pytest.mark.parametrize("verb", [lifecycle.revive, lifecycle.resume])
@pytest.mark.parametrize("ref", ["5e550001", "implementer-5e550001"])
def test_a_retired_session_is_never_revived_or_resumed(world: World, verb: Any, ref: str) -> None:
    world.seat("5e550001", task="t-0001", state=RETIRED, retired=STOPPED)
    assert rule(verb, world.ctx(), {"session": ref, "message": "carry on"}, None) == "retired"
    assert world.launcher.resumed == []


def test_a_bench_makes_room_only_for_a_task_that_outranks_the_one_it_replaces(world: World) -> None:
    world.queue.add("t-0001", goal_item="G1", status="building")
    world.seat("5e550001", task="t-0001")
    world.approve("t-0002", goal_item="G2")
    assert rule(lifecycle.bench, world.ctx(), {"session": "5e550001", "for": "t-0002"}, None) == "outranks"
    world.approve("t-0003", severity="severe")
    lifecycle.bench(world.ctx(), {"session": "5e550001", "for": "t-0003"}, None)
    assert world.session("5e550001").state == BENCH_PENDING


def test_a_bench_takes_a_waiting_session_first_and_never_a_merge_turn(world: World) -> None:
    world.cap(2)
    world.queue.add("t-0001", goal_item="G2", status="building")
    world.queue.add("t-0002", goal_item="G2", status="building")
    world.seat("3c3c3c3c", task="t-0001")
    world.seat("4d4d4d4d", task="t-0002", state=WAITING)
    world.approve("t-0003", severity="severe")
    bench = {"session": "3c3c3c3c", "for": "t-0003"}
    assert rule(lifecycle.bench, world.ctx(), bench, None) == "waiting-first"
    world.seat("4d4d4d4d", task="t-0002", state=WAITING, merge_turn=True)
    assert rule(lifecycle.bench, world.ctx(), {"session": "4d4d4d4d", "for": "t-0003"}, None) == "merge-turn"
    lifecycle.bench(world.ctx(), bench, None)


def test_a_benched_session_settles_after_its_grace_and_revives_with_its_id(world: World) -> None:
    world.queue.add("t-0001", goal_item="G2", status="building")
    benched_name = world.seat("5e550001", task="t-0001").name
    leases.claim(world.ctx(), "someone-else", "/w/x")  # slot 1
    assert leases.claim(world.ctx(), benched_name, "/w/b") == 2
    world.run("5e550001")
    world.approve("t-0002", severity="severe")
    lifecycle.bench(world.ctx(), {"session": "5e550001", "for": "t-0002"}, None)
    crashwatch.watch_sessions(world.ctx())
    assert world.session("5e550001").state == BENCH_PENDING and world.launcher.stopped == []
    world.later(world.settings.get_int("watch.bench_grace_seconds"))
    crashwatch.watch_sessions(world.ctx())
    benched = world.session("5e550001")
    assert benched.state == DEPRIORITIZED and leases.held_by(world.places, world.settings, benched_name) is None
    assert world.launcher.stopped == ["5e550001"] and world.launcher.stacks == [(2, ["empty"]), (2, [leases.SWEEP])]
    assert world.queue.tasks["t-0001"].status == "deprioritized"
    lifecycle.revive(world.ctx(), {"session": "5e550001"}, None)
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
    crashwatch.watch_sessions(world.ctx())
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
