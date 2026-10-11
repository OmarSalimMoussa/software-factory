"""The warden's scheduling pass: approved runs start, make room and come back from state, with no event to miss."""

from __future__ import annotations

from lifecycle_fakes import World

from runtime.queue import proposals
from runtime.shared import store
from runtime.shared.models import BENCH_PENDING, DEPRIORITIZED, WORKING
from runtime.warden import crashwatch, lifecycle, schedule


def _approved(world: World, *order: str) -> None:
    store.write_json(world.places.proposals, {"open": [], "approved": list(order)})


def _live(world: World, task_id: str) -> list[str]:
    return [s.state for s in lifecycle.read(world.ctx()).values() if s.task == task_id and not s.parent]


def test_an_approved_run_starts_on_the_next_pass_with_the_brief_it_was_proposed_with(world: World) -> None:
    world.approve("t-0001")
    _approved(world, "t-0001")
    store.write_text(lifecycle.brief_path(world.ctx(), "t-0001"), "Fix the loop; keep the old link.\n")

    schedule.run_pass(world.ctx())

    assert _live(world, "t-0001") == [WORKING] and world.queue.tasks["t-0001"].status == "building"
    assert world.launcher.started[0][1]["prompt"].endswith("Fix the loop; keep the old link.")


def test_a_full_cap_benches_the_lowest_ranked_run_the_new_one_outranks_then_starts_it(world: World) -> None:
    world.queue.add("t-0009", goal_item="G3", status="building")
    world.seat("5e550009", task="t-0009")
    world.approve("t-0001", goal_item="G1")
    _approved(world, "t-0001")

    schedule.run_pass(world.ctx())
    assert world.session("5e550009").state == BENCH_PENDING and _live(world, "t-0001") == []
    assert "pausing to make room" in schedule.waits(world.places)["t-0001"]

    world.later(world.settings.get_int("watch.bench_grace_seconds"))
    crashwatch.finish_benches(world.ctx(), {})
    schedule.run_pass(world.ctx())
    assert world.session("5e550009").state == DEPRIORITIZED and _live(world, "t-0001") == [WORKING]


def test_a_deprioritized_run_that_outranks_an_approved_one_comes_back_first(world: World) -> None:
    world.queue.add("t-0001", goal_item="G1", status="deprioritized", severity="severe")
    world.seat("5e550001", task="t-0001", state=DEPRIORITIZED)
    world.approve("t-0002", goal_item="G2")
    _approved(world, "t-0002")

    schedule.run_pass(world.ctx())

    assert world.session("5e550001").state == WORKING and world.launcher.resumed == ["5e550001"]
    assert _live(world, "t-0002") == [] and "hold a place" in schedule.waits(world.places)["t-0002"]


def test_a_designed_build_starts_its_implementer_and_a_run_that_cannot_start_says_why(world: World) -> None:
    world.approve("t-0001", run=3, slices=2, status="building")
    proposals.set_designed_builds(world.places, ["t-0001"])
    world.vitals("amber")

    schedule.run_pass(world.ctx())
    assert _live(world, "t-0001") == [] and "only severe work starts" in schedule.waits(world.places)["t-0001"]

    world.vitals("green")
    schedule.run_pass(world.ctx())
    assert _live(world, "t-0001") == [WORKING] and schedule.waits(world.places) == {}
