from __future__ import annotations

import logging
import threading

import pytest
from lifecycle_fakes import World, at_once

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
    WITH_PARENT,
    WORKING,
)
from runtime.warden import imports, inbox
from runtime.warden.sessions import backstop, crashwatch, lifecycle, permits
from runtime.warden.sessions.backstop import Proc
from runtime.warden.sessions.launch import LaunchError

NOW = "2026-10-09T12:00:00Z"


@pytest.mark.parametrize("state", [BENCH_PENDING, REVIVING, DEPRIORITIZED])
def test_the_crash_watch_never_reads_a_session_out_of_its_place_as_crashed(world: World, state: str) -> None:
    world.queue.add("t-0001", status="deprioritized")
    world.seat("5e550001", task="t-0001", state=state, since=NOW)
    for _ in range(3):
        crashwatch.SessionsWatch(start=at_once).run_pass(world.ctx())
    assert world.session("5e550001").state == state
    assert world.launcher.resumed == [] and world.launcher.stopped == []


@pytest.mark.parametrize("state", [BENCH_PENDING, DEPRIORITIZED])
def test_an_implementer_benched_at_its_merge_waits_for_its_check_and_ends_once_it_passes(
    world: World, state: str
) -> None:
    world.queue.add("t-0001", status="merged")
    world.seat("5e550001", task="t-0001", state=state, benched_for="t-0001", since=NOW)
    if state == BENCH_PENDING:
        world.run("5e550001")
    crashwatch.SessionsWatch(start=at_once).run_pass(world.ctx())
    assert world.session("5e550001").state == state

    world.queue.move(world.ctx(), "t-0001", "checked", "checked")
    crashwatch.SessionsWatch(start=at_once).run_pass(world.ctx())

    ended = world.session("5e550001")
    assert (ended.state, ended.retired) == (RETIRED, FINISHED)
    assert world.launcher.stopped == (["5e550001"] if state == BENCH_PENDING else [])  # stopped once, never benched


def test_a_session_whose_bench_is_settling_is_ended_only_once_it_settled(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.queue.add("t-0001", status="merged")
    world.seat("5e550001", task="t-0001", state=BENCH_PENDING, benched_for="t-0001")
    seen: list[str] = []

    def settle(ctx: object, s: object, procs: object) -> None:
        world.queue.move(world.ctx(), "t-0001", "checked", "checked")
        crashwatch.SessionsWatch(start=lambda work: None).run_pass(world.ctx())
        seen.append(world.session("5e550001").state)

    monkeypatch.setattr(crashwatch, "_settle", settle)
    crashwatch.finish_benches(world.ctx(), {})
    crashwatch.SessionsWatch(start=lambda work: None).run_pass(world.ctx())

    assert seen == [BENCH_PENDING] and world.session("5e550001").retired == FINISHED


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


def test_a_revived_sessions_process_naming_a_fresh_id_for_its_first_seconds_is_not_stopped(world: World) -> None:
    world.seat("44426cd6", name="orchestrator", role="orchestrator", family="orch", state=REVIVING, since=NOW)
    world.procs["82b353ac"] = Proc(pid=9, session="82b353ac", name="orchestrator", chat_link="")
    world.procs["9f9f9f9f"] = Proc(pid=10, session="9f9f9f9f", name="anything", chat_link="")
    backstop.sweep(world.ctx(), dict(world.procs))
    assert world.launcher.stopped == ["9f9f9f9f"]


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
        world.clock.at += 4 * world.settings.get_int("watch.resume_backoff_seconds")  # past each wait between them
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
    crashwatch.SessionsWatch(start=at_once).run_pass(world.ctx())
    assert world.launcher.resumed == []
    world.later(1)
    crashwatch.SessionsWatch(start=at_once).run_pass(world.ctx())
    world.later(world.settings.get_int("watch.tick_seconds"))
    crashwatch.SessionsWatch(start=at_once).run_pass(world.ctx())
    assert world.launcher.stopped == ["0c0c0c0c"] and world.launcher.resumed == ["0c0c0c0c"]
    assert world.session("0c0c0c0c").state == WORKING


def test_a_silent_orchestrator_restarted_as_a_copy_is_given_its_time_again(world: World) -> None:
    world.seat("0c0c0c0c", role="orchestrator", family="orch", since=NOW, definitions="older")
    world.run("0c0c0c0c")
    world.later(world.settings.get_int("watch.orchestrator_busy_silent_minutes") * 60)
    crashwatch.SessionsWatch(start=at_once).run_pass(world.ctx())
    world.later(world.settings.get_int("watch.tick_seconds"))
    crashwatch.SessionsWatch(start=at_once).run_pass(world.ctx())
    assert world.launcher.stopped == ["0c0c0c0c"] and world.session("copy-0c0c0c0c").state == WORKING


def test_an_orchestrator_waiting_on_its_inbox_is_alive_however_long_since_its_last_loop_pass(world: World) -> None:
    world.seat("0c0c0c0c", role="orchestrator", family="orch", since=NOW)
    world.run("0c0c0c0c")
    world.later(world.settings.get_int("watch.orchestrator_busy_silent_minutes") * 60 - 60)
    inbox.post_event(world.places, world.clock, inbox.TRIAGE_DUE, {"waiting": 3})
    inbox.wait(world.ctx(), {}, None)
    world.later(120)

    crashwatch.SessionsWatch(start=at_once).run_pass(world.ctx())

    assert world.launcher.resumed == []


def test_a_restarted_orchestrator_is_told_the_events_it_has_not_acted_on(world: World) -> None:
    world.seat("0c0c0c0c", role="orchestrator", family="orch")
    inbox.post_event(world.places, world.clock, inbox.TRIAGE_DUE, {"waiting": 3})
    crashwatch.watch(world.ctx())
    [(_, said)] = world.launcher.messages
    assert "Events you have not acted on yet: triage due." in said


def test_while_sessions_run_unseen_the_crash_watch_resumes_and_retires_none(world: World) -> None:
    world.queue.add("t-0001", status="building")
    world.seat("5e550001", task="t-0001")
    blind = {
        "at": NOW,
        "level": "red",
        "why": "sessions: 1 unseen",
        "readings": [{"name": "sessions", "level": "red", "value": "1 unseen", "figure": None}],
    }
    store.write_json(world.ctx().places.vitals, blind)

    for _ in range(world.settings.get_int("watch.resume_tries") + 2):
        crashwatch.watch(world.ctx())

    assert world.session("5e550001").state == WORKING and world.launcher.resumed == []


def test_sessions_that_ended_with_their_daemon_come_back_without_counting_as_crashed(world: World) -> None:
    world.queue.add("t-0001", status="building")
    world.seat("5e550001", task="t-0001")
    crashwatch.watch(world.ctx())  # a crash: the first resume counts
    for _ in range(3):  # then the machine reboots three times, each a new daemon, and the session is gone each time
        world.procs.clear()
        world.clock.at += 3600
        world.daemon = world.clock.now() - 60
        crashwatch.watch(world.ctx())

    s = world.session("5e550001")
    assert (s.state, s.resume_tries, world.launcher.resumed.count("5e550001")) == (WORKING, 1, 4)


@pytest.mark.parametrize("daemon", [None, 1.0], ids=["not-running", "just-started"])
def test_nothing_resumes_until_the_daemon_is_up_and_settled(world: World, daemon: float | None) -> None:
    world.queue.add("t-0001", status="building")
    world.seat("5e550001", task="t-0001")
    world.daemon = None if daemon is None else world.clock.now() - daemon

    crashwatch.watch(world.ctx())

    assert world.launcher.resumed == [] and world.session("5e550001").resume_tries == 0
    world.daemon = world.clock.now() - 3600
    crashwatch.watch(world.ctx())
    assert world.launcher.resumed == ["5e550001"]


def test_a_crash_waits_longer_each_time_and_one_long_after_the_last_counts_afresh(world: World) -> None:
    world.queue.add("t-0001", status="building")
    world.seat("5e550001", task="t-0001")
    backoff = world.settings.get_int("watch.resume_backoff_seconds")

    def crash() -> None:  # each resume dies again at once
        world.procs.clear()
        crashwatch.watch(world.ctx())

    crash()
    crash()  # it died again at once: the next resume waits
    assert world.launcher.resumed == ["5e550001"]

    world.clock.at += backoff
    crash()
    assert world.launcher.resumed == ["5e550001"] * 2 and world.session("5e550001").resume_tries == 2

    world.clock.at += world.settings.get_int("watch.crash_window_minutes") * 60 + 1
    crash()  # two crashes are the limit, but this one comes long after them
    s = world.session("5e550001")
    assert (s.state, s.resume_tries, world.launcher.resumed.count("5e550001")) == (WORKING, 1, 3)


def test_a_slow_bench_settles_beside_the_pass_and_no_second_one_starts_meanwhile(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    started, done, runs = threading.Event(), threading.Event(), []

    def slow(ctx: object, procs: object) -> None:  # a slot's scripts/dev down that takes long
        runs.append(1)
        started.set()
        done.wait(10)

    monkeypatch.setattr(crashwatch, "finish_benches", slow)
    watcher = crashwatch.SessionsWatch(imports.in_thread("benches"))
    watcher.run_pass(world.ctx())
    assert started.wait(10)
    watcher.run_pass(world.ctx())  # returns at once, though the first bench still runs

    assert runs == [1]
    done.set()


def test_a_bench_settling_that_fails_in_its_thread_is_reported_there_once_while_it_lasts(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(ctx: object, procs: object) -> None:
        raise store.StoreError("registry.json: not an object")

    monkeypatch.setattr(crashwatch, "finish_benches", broken)
    watcher = crashwatch.SessionsWatch(at_once)
    with caplog.at_level(logging.ERROR):
        for _ in range(2):
            watcher.run_pass(world.ctx())
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == [
        "warden: the benches' settling failed (StoreError)"
    ]


def test_a_session_the_warden_is_launching_is_never_launched_a_second_time(world: World) -> None:
    world.queue.add("t-0001", status="building")
    world.seat("5e550001", task="t-0001")
    with world.ctx().lock:
        assert lifecycle.claim_launch(world.ctx(), "5e550001")
    try:
        crashwatch.watch(world.ctx())
        with pytest.raises(LaunchError, match="bringing implementer-5e550001 back right now"):
            lifecycle.deliver(world.owner(), "5e550001", "Carry on.", "owner")
    finally:
        lifecycle.release_launch(world.ctx(), "5e550001")

    assert world.launcher.resumed == []


def test_a_resumed_session_takes_the_subagents_of_its_ended_process_out_of_the_running(world: World) -> None:
    world.queue.add("t-0001", status="qa")
    world.seat("5e550001", task="t-0001")
    world.seat("a1b2c3", role="qa-ux", family="qa", task="t-0001", parent="5e550001", agent_id="a1b2c3")

    crashwatch.watch(world.ctx())  # the implementer's process ended: the warden resumes it

    tester = world.session("a1b2c3")
    assert (tester.state, tester.retired) == (RETIRED, WITH_PARENT) and world.launcher.resumed == ["5e550001"]
