from __future__ import annotations

import pytest
from lifecycle_fakes import World

from runtime.shared import store
from runtime.shared.models import (
    BENCH_PENDING,
    CRASHED,
    DEPRIORITIZED,
    FINISHED,
    RETIRED,
    REVIVING,
    STOPPED,
    WAITING,
    WORKING,
)
from runtime.warden import backstop, crashwatch, inbox, lifecycle, permits
from runtime.warden.backstop import Proc

NOW = "2026-10-09T12:00:00Z"


@pytest.mark.parametrize("state", [BENCH_PENDING, REVIVING, DEPRIORITIZED])
def test_the_crash_watch_never_reads_a_session_out_of_its_place_as_crashed(world: World, state: str) -> None:
    world.queue.add("t-0001", status="deprioritized")
    world.seat("5e550001", task="t-0001", state=state, since=NOW)
    for _ in range(3):
        crashwatch.watch_sessions(world.ctx())
    assert world.session("5e550001").state == state
    assert world.launcher.resumed == [] and world.launcher.stopped == []


@pytest.mark.parametrize(
    ("state", "stopped"),
    [(WORKING, False), (WAITING, False), (BENCH_PENDING, False), (REVIVING, False), (DEPRIORITIZED, True),
     (RETIRED, True)],
)  # fmt: skip
def test_the_backstop_stops_only_processes_the_warden_did_not_permit(world: World, state: str, stopped: bool) -> None:
    world.seat("5e550001", task="t-0001", state=state, since=NOW, retired=STOPPED if state == RETIRED else "")
    world.run("5e550001")
    world.procs["9f9f9f9f"] = Proc(pid=9, session="9f9f9f9f", name="anything", chat_link="")
    backstop.sweep(world.ctx(), dict(world.procs))
    assert world.launcher.stopped == (["5e550001", "9f9f9f9f"] if stopped else ["9f9f9f9f"])


def test_a_start_is_bound_to_its_session_by_name_and_its_pending_id_still_names_it(world: World) -> None:
    world.seat("pending-abc", name="impl-t-0001", task="t-0001", since=NOW)
    world.procs["0f00ba12-real"] = Proc(pid=7, session="0f00ba12-real", name="impl-t-0001", chat_link="")
    assert backstop.sweep(world.ctx(), dict(world.procs)) == []
    assert world.session("0f00ba12-real").name == "impl-t-0001"
    assert lifecycle.find(world.ctx(), lifecycle.read(world.ctx()), "pending-abc").id == "0f00ba12-real"


def test_a_crashed_session_is_resumed_then_retired_but_the_orchestrator_never(world: World) -> None:
    world.queue.add("t-0001", status="building")
    world.seat("5e550001", task="t-0001")
    world.seat("0c0c0c0c", role="orchestrator", family="orch")
    tries = world.settings.get_int("watch.resume_tries")
    for _ in range(tries + 2):
        world.procs.clear()  # each resume dies again at once
        crashwatch.watch(world.ctx())
    crashed = world.session("5e550001")
    assert (crashed.state, crashed.retired, crashed.retired_by) == (RETIRED, CRASHED, "warden")
    assert world.launcher.resumed.count("5e550001") == tries
    assert world.session("0c0c0c0c").state == WORKING and world.launcher.resumed.count("0c0c0c0c") == tries + 2
    assert world.queue.tasks["t-0001"].status == "proposed"


@pytest.mark.parametrize(
    ("role", "status"),
    [("implementer", "checked"), ("implementer", "promoted"), ("designer", "building")],
    ids=["implementer-checked", "implementer-on-prod", "designer-after-go"],
)
@pytest.mark.parametrize("alive", [True, False], ids=["idle-process", "process-gone"])
def test_a_session_whose_task_moved_past_its_part_is_retired_as_finished_and_stopped(
    world: World, role: str, status: str, alive: bool
) -> None:
    world.queue.add("t-0001", status=status)
    world.seat("5e550001", role=role, task="t-0001")
    if alive:
        world.run("5e550001")  # a background session stays alive when idle
    crashwatch.watch(world.ctx())
    assert (world.session("5e550001").retired, world.launcher.resumed) == (FINISHED, [])
    assert world.launcher.stopped == (["5e550001"] if alive else [])
    assert not permits.cap_full(world.settings, lifecycle.read(world.ctx()).values(), role)  # its place is free


def test_a_silent_orchestrator_is_restarted_as_the_same_session(world: World) -> None:
    world.seat("0c0c0c0c", role="orchestrator", family="orch", since=NOW)
    world.run("0c0c0c0c")
    quiet = world.settings.get_int("watch.orchestrator_busy_silent_minutes") * 60
    world.later(quiet - 1)
    crashwatch.watch_sessions(world.ctx())
    assert world.launcher.resumed == []
    world.later(1)
    crashwatch.watch_sessions(world.ctx())
    world.later(world.settings.get_int("watch.tick_seconds"))
    crashwatch.watch_sessions(world.ctx())
    assert world.launcher.stopped == ["0c0c0c0c"] and world.launcher.resumed == ["0c0c0c0c"]
    assert world.session("0c0c0c0c").state == WORKING


def test_a_restarted_orchestrator_is_told_the_events_it_has_not_acted_on(world: World) -> None:
    world.seat("0c0c0c0c", role="orchestrator", family="orch")
    inbox.post_event(world.places, world.clock, inbox.TRIAGE_DUE, {"waiting": 3})
    crashwatch.watch(world.ctx())
    resumed = next(argv for argv in world.launcher.commands if argv[:2] == ["claude", "--resume"])
    assert "Events you have not acted on yet: triage due." in resumed[-1]


def test_while_sessions_run_unseen_the_crash_watch_resumes_and_retires_none(world: World) -> None:
    world.queue.add("t-0001", status="building")
    world.seat("5e550001", task="t-0001")
    blind = {"at": NOW, "level": "red", "readings": [{"name": "sessions", "level": "red", "value": "1 unseen"}]}
    store.write_json(world.ctx().places.vitals, blind)

    for _ in range(world.settings.get_int("watch.resume_tries") + 2):
        crashwatch.watch(world.ctx())

    assert world.session("5e550001").state == WORKING and world.launcher.resumed == []
