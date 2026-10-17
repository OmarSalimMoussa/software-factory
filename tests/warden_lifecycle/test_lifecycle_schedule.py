"""The warden's scheduling pass: approved runs start, make room and come back from state, with no event to miss."""

from __future__ import annotations

import pytest
from lifecycle_fakes import World

from runtime.queue import proposals
from runtime.shared import store
from runtime.shared.models import BENCH_PENDING, DEPRIORITIZED, STOPPED, WORKING
from runtime.warden import schedule
from runtime.warden.sessions import crashwatch, lifecycle


@pytest.fixture(autouse=True)
def orchestrator(world: World) -> None:
    """Runs start only in a working factory: one with its orchestrator at work."""
    world.seat("0c0c0c0c", role="orchestrator", family="orch")


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


def test_an_approved_task_whose_run_still_holds_its_place_goes_on_building_with_that_run(world: World) -> None:
    world.approve("t-0001")
    _approved(world, "t-0001")
    world.seat("5e550001", task="t-0001")  # a facts change voided its approval while it ran

    schedule.run_pass(world.ctx())

    assert world.queue.tasks["t-0001"].status == "building" and _live(world, "t-0001") == [WORKING]
    assert world.launcher.started == []


def test_a_word_held_for_a_run_reaches_its_next_start_after_the_brief_and_only_once(world: World) -> None:
    world.approve("t-0001")
    _approved(world, "t-0001")
    store.write_text(lifecycle.brief_path(world.ctx(), "t-0001"), "Fix the loop.\n")
    store.write_text(lifecycle.held_path(world.ctx(), "t-0001"), "Your work was taken back off master.\n")

    schedule.run_pass(world.ctx())

    assert world.launcher.started[0][1]["prompt"].endswith("Fix the loop.\n\nYour work was taken back off master.")
    assert not lifecycle.held_path(world.ctx(), "t-0001").exists()


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


@pytest.mark.parametrize(
    ("status", "back", "by"),
    [("merged", "reverted", "orchestrator"), ("blocked", "unblocked", "owner"), ("on-hold", "released", "warden")],
)
def test_an_implementer_benched_at_its_merge_holds_no_place_and_comes_back_when_its_task_is_sent_back(
    world: World, status: str, back: str, by: str
) -> None:
    world.queue.add("t-0001", goal_item="G1", status=status, severity="severe")
    world.seat("5e550001", task="t-0001", state=DEPRIORITIZED, benched_for="t-0001")
    world.approve("t-0002", goal_item="G2")
    _approved(world, "t-0002")

    schedule.run_pass(world.ctx())
    assert world.session("5e550001").state == DEPRIORITIZED and _live(world, "t-0002") == [WORKING]

    # Its check failed, the owner fixed its block, or the task it waited for landed.
    world.queue.move(world.ctx(), "t-0001", "building", back, by)
    world.cap(2)
    schedule.run_pass(world.ctx())
    assert world.session("5e550001").state == WORKING and world.launcher.resumed == ["5e550001"]


def test_a_merge_bench_holds_no_start_back_while_a_place_is_free(world: World) -> None:
    world.cap(2)
    world.queue.add("t-0001", status="merged")
    world.seat("5e550001", task="t-0001", state=BENCH_PENDING, benched_for="t-0001", since="2026-10-09T12:00:00Z")
    world.approve("t-0002")
    _approved(world, "t-0002")

    schedule.run_pass(world.ctx())

    assert _live(world, "t-0002") == [WORKING]


def test_a_designer_benched_while_its_spec_waits_for_review_comes_back_when_a_place_frees(world: World) -> None:
    world.queue.add("t-0001", status="review", run=3)
    world.seat("de510001", role="designer", task="t-0001", state=DEPRIORITIZED, benched_for="t-0002")

    schedule.run_pass(world.ctx())

    assert world.session("de510001").state == WORKING


def test_a_designed_build_starts_its_implementer_and_a_run_that_cannot_start_says_why(world: World) -> None:
    world.approve("t-0001", run=3, slices=2, status="building")
    proposals.set_designed_builds(world.places, ["t-0001"])
    world.vitals("amber", "power: on battery, 25%")

    schedule.run_pass(world.ctx())
    assert _live(world, "t-0001") == []
    assert "amber (power: on battery, 25%), so only severe work starts" in schedule.waits(world.places)["t-0001"]

    world.vitals("green")
    schedule.run_pass(world.ctx())
    assert _live(world, "t-0001") == [WORKING] and schedule.waits(world.places) == {}


def test_a_factory_the_owner_stopped_starts_no_run(world: World) -> None:
    world.approve("t-0001")
    _approved(world, "t-0001")
    lifecycle.retire_session(world.ctx(), "0c0c0c0c", STOPPED, "owner")

    schedule.run_pass(world.ctx())

    assert _live(world, "t-0001") == [] and world.launcher.started == []
