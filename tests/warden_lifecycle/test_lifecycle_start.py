from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest
from lifecycle_fakes import World

from runtime.shared import store
from runtime.shared.models import DEPRIORITIZED, RETIRED, WAITING, WORKING
from runtime.shared.protocol import Refused
from runtime.warden.delivery import outage
from runtime.warden.health import vitals
from runtime.warden.sessions import lifecycle, reap

START: dict[str, Any] = {"role": "implementer", "task": "t-0001"}


def refusal(world: World, args: dict[str, Any] = START) -> str:
    with pytest.raises(Refused) as refused:
        lifecycle.start(world.ctx(), args, None)
    return refused.value.rule


def test_a_start_needs_an_approval_that_still_covers_the_task(world: World) -> None:
    task = world.queue.add("t-0001")
    assert refusal(world) == "approval"
    world.approve()
    world.queue.tasks["t-0001"] = dataclasses.replace(task, title="A different change")
    assert refusal(world) == "approval"
    world.queue.tasks["t-0001"] = task
    answer = lifecycle.start(world.ctx(), START, None)
    assert world.session(answer["session"]).state == WORKING
    assert world.queue.tasks["t-0001"].status == "building"
    assert world.launcher.started[0][1]["model"] == world.settings.model("implementer_small")[0]


@pytest.mark.parametrize(
    "facts", [{"owner_class": "migration"}, {"surfaces": ["api", "access"]}, {"cause": ""}], ids=str
)
def test_a_start_is_refused_when_the_facts_now_need_a_higher_rung_than_was_approved(
    world: World, facts: dict[str, Any]
) -> None:
    task = world.approve()  # rung 1: a small fix with its cause at a line
    world.queue.tasks["t-0001"] = dataclasses.replace(task, **facts)  # intake learned more after the approval

    assert refusal(world) == "rung"


@pytest.mark.parametrize(
    ("approved", "facts", "starts"),
    [({"run": 2}, {"surfaces": ["api", "access"]}, True), ({"run": 2}, {"cause": ""}, False),
     ({"run": 3, "status": "building"}, {"slices": 2}, True)],
    ids=["route-2-under-rung-2", "route-3-over-rung-2", "built-at-rung-3"],
)  # fmt: skip
def test_a_run_approved_above_its_route_starts_while_its_facts_route_no_higher_than_the_run(
    world: World, approved: dict[str, Any], facts: dict[str, Any], starts: bool
) -> None:
    task = world.approve(**approved)  # its facts route to rung 1, and the orchestrator proposed more with its reason
    world.queue.tasks["t-0001"] = dataclasses.replace(task, **facts)

    if starts:
        lifecycle.start(world.ctx(), START, None)
    else:
        assert refusal(world) == "rung"


def test_a_run_proposed_below_its_route_with_a_reason_still_starts(world: World) -> None:
    world.approve(cause="")  # its facts route to rung 3, and the orchestrator proposed rung 1 with its reason

    answer = lifecycle.start(world.ctx(), START, None)

    assert world.session(answer["session"]).state == WORKING


@pytest.mark.parametrize(
    ("other", "refused"), [(WORKING, True), (WAITING, True), (DEPRIORITIZED, False), (RETIRED, False)]
)
def test_caps_count_only_working_and_waiting_sessions(world: World, other: str, refused: bool) -> None:
    world.queue.add("t-0009", goal_item="G2", status="deprioritized" if other == DEPRIORITIZED else "building")
    world.seat("s-other", task="t-0009", state=other)
    world.approve()
    if refused:
        assert refusal(world) == "cap"
    else:
        lifecycle.start(world.ctx(), START, None)


@pytest.mark.parametrize(
    ("level", "severity", "starts"),
    [("red", "severe", False), ("amber", "major", False), ("unknown", "major", False), ("amber", "severe", True)],
)
def test_red_vitals_block_every_start_and_amber_all_but_severe(
    world: World, level: str, severity: str, starts: bool
) -> None:
    world.approve(severity=severity)
    world.vitals(level)
    if starts:
        lifecycle.start(world.ctx(), START, None)
    else:
        assert refusal(world) == "vitals"


@pytest.mark.parametrize(("recorded", "starts"), [(True, True), (False, False)])
def test_a_red_install_is_read_again_at_a_start_since_an_upgrade_records_it_after_the_new_warden_reads(
    world: World, recorded: bool, starts: bool
) -> None:
    world.approve()
    world.places.install.mkdir(parents=True, exist_ok=True)
    (world.places.install / "COMMIT").write_text("abc\n")
    world.places.install_hash.write_text(vitals.install_digest(world.places.install) if recorded else "before")
    readings = [vitals.Reading("install", vitals.RED, "differs"), vitals.Reading("memory", vitals.GREEN, "4 GiB")]
    store.write_json(world.places.vitals, {"level": vitals.RED, "why": "install: differs",
                                           "readings": [dataclasses.asdict(r) for r in readings]})  # fmt: skip
    if starts:
        lifecycle.start(world.ctx(), START, None)
    else:
        assert refusal(world) == "vitals"


@pytest.mark.parametrize(("severity", "starts"), [("normal", False), ("severe", True)])
def test_a_day_whose_spend_reached_the_budget_starts_only_severe_work(
    world: World, severity: str, starts: bool
) -> None:
    world.approve(severity=severity)
    today = world.clock.iso()[:10]
    store.write_json(
        world.places.state / "cost-days.json", {today: [world.settings.get_float("cost.daily_budget_usd"), 3]}
    )
    if starts:
        lifecycle.start(world.ctx(), START, None)
    else:
        assert refusal(world) == "budget"


@pytest.mark.parametrize(
    "remote", ["git@github.com:someone/software-factory.git", "https://github.com/x/SOFTWARE-FACTORY"]
)
def test_no_session_starts_on_the_factory_own_repo(world: World, remote: str) -> None:
    world.approve()
    world.launcher.remote = remote
    assert refusal(world) == "own-repo"


def test_the_outage_bypass_needs_the_warden_own_record_and_runs_once(world: World) -> None:
    world.queue.add("t-0001", severity="severe", status="queued")
    bypass = {**START, "outage": True}
    assert refusal(world, bypass) == "outage"
    store.write_json(outage.path(world.places), {"since": "2026-10-09T11:58:00Z", "stage": "down"})
    lifecycle.start(world.ctx(), bypass, None)
    record = outage.read(world.places)
    assert record is not None and record.bypass_used and record.stage == "fixing"
    world.queue.add("t-0002", severity="severe", status="queued")
    assert refusal(world, {**bypass, "task": "t-0002"}) == "outage"


def test_a_deprioritized_session_comes_back_before_a_run_it_outranks(world: World) -> None:
    world.queue.add("t-0009", goal_item="G1", status="deprioritized", created="2026-09-01T00:00:00Z")
    world.seat("s-benched", task="t-0009", state=DEPRIORITIZED)
    world.approve(goal_item="G2")
    world.cap(2)
    assert refusal(world) == "revive-first"


def test_a_new_session_takes_over_the_worktree_the_reap_held_for_its_task(world: World) -> None:
    world.approve()
    worktree = Path(world.settings.get_str("repo.path")) / ".claude" / "worktrees" / "design-t-0001"
    worktree.mkdir(parents=True)
    world.seat("5e550001", task="t-0001", state=RETIRED, retired="crashed", worktree=str(worktree))
    reap.reap(world.ctx(), "5e550001")
    assert reap.held_for(world.places, "t-0001") is not None
    answer = lifecycle.start(world.ctx(), START, None)
    assert world.session(answer["session"]).worktree == str(worktree)
    assert world.launcher.started[0][1]["worktree"] == "design-t-0001"
    assert reap.held_for(world.places, "t-0001") is None
