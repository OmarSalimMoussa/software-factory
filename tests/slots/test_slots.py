"""Stack slots: a lease per implementer, worked out from the caller and never from an argument; a desktop each slot's
tester holds alone, within the machine's cap and its CPU hold, brought up once the other browser walks hand back."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from queue_kit import World

from runtime.guard.facts import read_desktops
from runtime.shared import store
from runtime.shared.models import RETIRED
from runtime.shared.protocol import Refused
from runtime.slots import leases
from runtime.slots import verbs as slots_verbs
from runtime.slots.verbs import slot
from runtime.warden import public
from runtime.warden.context import StackResult
from runtime.warden.sessions import reap, registry


def _implementer(world: World, n: int, status: str = "") -> None:
    task = world.task()
    if status:
        world.queue().write_task(dataclasses.replace(task, status=status, run=1))
    world.session(f"s-{n}", "implementer", f"impl-{task.id}", task.id, f"/wt/impl-{task.id}")


def test_claim_takes_the_first_free_slot_and_keeps_it(world: World) -> None:
    world.with_settings(slots__count=2)
    for n in (1, 2, 3):
        _implementer(world, n)
    assert slot(world.ctx("implementer", "s-1"), {"op": "claim"}, None)["slot"] == 1
    assert slot(world.ctx("implementer", "s-1"), {"op": "claim"}, None)["slot"] == 1
    assert slot(world.ctx("implementer", "s-2"), {"op": "claim", "note": "a\nnote"}, None)["slot"] == 2
    lease = leases.read(world.places, 2)
    assert lease is not None and (lease.holder, lease.note) == ("impl-t-0002", "a note")
    with pytest.raises(Refused) as refused:
        slot(world.ctx("implementer", "s-3"), {"op": "claim"}, None)
    assert refused.value.rule == "slots-full"


def test_an_implementer_whose_task_is_qa_clean_claims_no_slot_before_its_merge_turn(world: World) -> None:
    _implementer(world, 1, "qa-clean")
    with pytest.raises(Refused, match="waits for its merge turn, which needs no slot"):
        slot(world.ctx("implementer", "s-1"), {"op": "claim"}, None)
    assert leases.read(world.places, 1) is None


def test_a_stack_command_before_a_claim_is_refused(world: World) -> None:
    _implementer(world, 1)
    with pytest.raises(Refused, match="claim a slot first"):
        slot(world.ctx("implementer", "s-1"), {"op": "up"}, None)
    assert world.launcher.stacks == []


def test_the_slot_comes_from_the_lease_never_from_an_argument(world: World) -> None:
    _implementer(world, 1)
    _implementer(world, 2)
    slot(world.ctx("implementer", "s-1"), {"op": "claim"}, None)
    slot(world.ctx("implementer", "s-2"), {"op": "claim"}, None)
    world.launcher.stack = lambda n, verb: StackResult(0, "done\n", "/log")
    answer = slot(world.ctx("implementer", "s-2"), {"op": "up", "slot": 1, "services": ["web"]}, None)
    assert answer["slot"] == 2 and world.launcher.stacks == [(2, ["up", "web"])]


@pytest.mark.parametrize(
    ("args", "agent_id"),
    [
        ({"op": "up", "services": ["--build"]}, ""),  # an option smuggled as a service
        ({"op": "sign-in", "email": "x; rm -rf /"}, ""),
        ({"op": "seed", "seed": ["load", "-x"]}, ""),
        ({"op": "up"}, "agent-qa-1"),  # a tester signs in and opens the desktop; the stack is its parent's
        ({"op": "seed", "seed": ["load", "cart"]}, "agent-qa-1"),
        ({"op": "desktop", "desktop": ["down"]}, "agent-qa-1"),
        ({"op": "run", "service": "web", "command": ["ls"]}, "agent-qa-1"),
        ({"op": "run", "service": "--rm", "command": ["ls"]}, ""),
        ({"op": "run", "service": "web", "command": []}, ""),
        ({"op": "restart", "services": []}, ""),
        ({"op": "restart", "services": ["--force-recreate"]}, ""),
        ({"op": "restart", "services": ["web"]}, "agent-qa-1"),
        ({"op": "stop", "services": []}, ""),
        ({"op": "stop", "services": ["--timeout"]}, ""),
        ({"op": "stop", "services": ["web"]}, "agent-qa-1"),
    ],
)
def test_only_checked_words_reach_scripts_dev(world: World, args: dict[str, object], agent_id: str) -> None:
    _implementer(world, 1)
    slot(world.ctx("implementer", "s-1"), {"op": "claim"}, None)
    with pytest.raises(Refused):
        slot(world.ctx("implementer", "s-1", agent_id), args, None)
    assert world.launcher.stacks == []


def test_slot_0_is_staging_and_comes_up_whole(world: World) -> None:
    world.session("s-orch", "orchestrator", "orchestrator")
    world.launcher.stack = lambda n, verb: StackResult(0, "up\n", "/log")

    with pytest.raises(Refused, match="one build of the whole stack"):
        slot(world.ctx(), {"op": "up", "services": ["control-plane"]}, None)
    slot(world.ctx(), {"op": "up"}, None)

    assert world.launcher.stacks == [(0, ["up"])]


def _git(ancestor: int) -> Callable[[list[str]], tuple[int, str]]:
    """git in the checkout: every sha names commit FULL, an ancestor of the default branch when ``ancestor`` is 0."""

    def run(argv: list[str]) -> tuple[int, str]:
        answers = {"rev-parse": (0, f"{FULL}\n"), "merge-base": (ancestor, ""), "remove": (128, "not a working tree")}
        return answers.get(argv[3], answers.get(argv[4], (0, "")))

    return run


FULL = "a" * 40


def test_slot_0_comes_up_at_an_older_commit_from_its_own_worktree_then_back_at_the_tip(world: World) -> None:
    world.session("s-orch", "orchestrator", "orchestrator")
    world.launcher.git = _git(0)
    where = str(world.repo / ".claude" / "worktrees" / "staging-at")

    assert slot(world.ctx(), {"op": "up", "at": "aaaaaaa"}, None)["at"] == FULL
    assert [argv[3:] for argv in world.launcher.commands if argv[3] == "worktree"] == [
        ["worktree", "remove", "--force", where], ["worktree", "prune"],
        ["worktree", "add", "--force", "--detach", where, FULL],
    ]  # fmt: skip
    assert store.read_json(public.staging_at_path(world.places)) == {"commit": FULL}
    assert slot(world.ctx(), {"op": "up"}, None)["at"] is None
    assert not public.staging_at_path(world.places).exists()
    assert world.launcher.stacks == [(0, ["up"]), (0, ["up"])]


@pytest.mark.parametrize(
    ("args", "ancestor", "said"),
    [
        ({"op": "up", "at": "bbbbbbb"}, 1, "bbbbbbb is not on master"),
        ({"op": "up", "at": "HEAD~1"}, 0, "names a commit of the default branch by its sha"),
        ({"op": "down", "at": "aaaaaaa"}, 0, "only slot 0's up takes --at"),
    ],
)
def test_slot_0_comes_up_only_at_a_commit_of_the_default_branch(
    world: World, args: dict[str, object], ancestor: int, said: str
) -> None:
    world.session("s-orch", "orchestrator", "orchestrator")
    world.launcher.git = _git(ancestor)

    with pytest.raises(Refused, match=said):
        slot(world.ctx(), args, None)
    assert world.launcher.stacks == [] and not public.staging_at_path(world.places).exists()


@pytest.mark.parametrize(
    "args", [{"op": "up"}, {"op": "down"}, {"op": "restart", "services": ["web"]}, {"op": "stop", "services": ["web"]}]
)
def test_no_stack_changes_under_a_tester_still_walking_the_slot(world: World, args: dict[str, object]) -> None:
    _implementer(world, 1)
    slot(world.ctx("implementer", "s-1"), {"op": "claim"}, None)
    world.session("t-1", "qa-ui-function", "qa-ui-function of impl-t-0001", agent_id="agent-qa-1", parent="s-1")
    world.launcher.stack = lambda n, verb: StackResult(0, "done\n", "/log")

    with pytest.raises(Refused, match="qa-ui-function of impl-t-0001 still testing slot 1"):
        slot(world.ctx("implementer", "s-1"), args, None)
    sessions = registry.read(world.places)
    sessions["t-1"].state = RETIRED
    registry.write(world.places, sessions)
    slot(world.ctx("implementer", "s-1"), args, None)

    assert len([verb for _, verb in world.launcher.stacks if verb != ["health"]]) == 1


@pytest.mark.parametrize("op", ["restart", "stop"])
def test_the_holder_restarts_or_stops_the_services_it_names(world: World, op: str) -> None:
    _implementer(world, 1)
    slot(world.ctx("implementer", "s-1"), {"op": "claim"}, None)

    slot(world.ctx("implementer", "s-1"), {"op": op, "services": ["web", "worker"]}, None)

    assert world.launcher.stacks[-1] == (1, [op, "web", "worker"])


def test_a_slots_own_session_and_its_backend_and_security_testers_run_any_command_in_it_and_no_other_tester_does(
    world: World,
) -> None:
    _implementer(world, 1)
    world.session("s-orch", "orchestrator", "orchestrator")
    slot(world.ctx("implementer", "s-1"), {"op": "claim"}, None)
    world.launcher.stack = lambda n, verb: StackResult(0, "4.0K /app\n", "/log")
    command = ["sh", "-c", "du -sh /app; rm -f /tmp/x"]
    run = {"op": "run", "service": "web", "command": command}

    answer = slot(world.ctx("implementer", "s-1"), run, None)
    for agent_id, kind in (("agent-api-1", "qa-backend"), ("agent-sec-1", "qa-security")):
        slot(world.ctx("implementer", "s-1", agent_id, kind), run, None)

    slot(world.ctx(), run, None)

    assert answer["tail"] == "4.0K /app\n" and world.launcher.stacks == [
        *[(1, ["run", "web", "--", *command])] * 3,
        (0, ["run", "web", "--", *command]),
    ]
    with pytest.raises(Refused, match="a qa-regression may run"):
        slot(world.ctx("orchestrator", "s-orch", "agent-reg-1", "qa-regression"), run, None)
    with pytest.raises(Refused, match="a qa-ux may run"):
        slot(world.ctx("implementer", "s-1", "agent-ux-1", "qa-ux"), run, None)


def test_a_slots_session_and_any_of_its_testers_read_what_runs_on_it(world: World) -> None:
    _implementer(world, 1)
    slot(world.ctx("implementer", "s-1"), {"op": "claim"}, None)
    world.launcher.stack = lambda n, verb: StackResult(0, "notes-slot1-web 2.0% CPU, 90MiB / 1GiB\n", "/log")

    for caller in (world.ctx("implementer", "s-1"), world.ctx("implementer", "s-1", "agent-ux-1", "qa-ux")):
        assert "2.0% CPU" in slot(caller, {"op": "top"}, None)["tail"]

    assert world.launcher.stacks == [(1, [leases.TOP])] * 2


@pytest.mark.parametrize(
    ("exit_code", "woke", "noted"),
    [(1, "2026-10-09T11:55:00Z", True), (1, "2026-10-09T11:00:00Z", False), (0, "2026-10-09T11:55:00Z", False)],
    ids=["failed-after-a-wake", "failed-long-after", "worked"],
)
def test_a_slot_command_that_fails_soon_after_the_laptop_wakes_says_so(
    world: World, exit_code: int, woke: str, noted: bool
) -> None:
    _implementer(world, 1)
    slot(world.ctx("implementer", "s-1"), {"op": "claim"}, None)
    store.write_json(world.places.woke, {"at": woke})
    world.launcher.stack = lambda n, verb: StackResult(exit_code, "Failed to resolve 'pypi.org'\n", "/log")

    answer = slot(world.ctx("implementer", "s-1"), {"op": "up"}, None)

    # A build cut off by the laptop's sleep read as a factory DNS fault, and was reported as one.
    assert ("woke from sleep 5 min ago" in answer.get("note", "")) is noted


def test_the_orchestrator_frees_only_a_finished_sessions_slot(world: World) -> None:
    _implementer(world, 1)
    world.session("s-orch", "orchestrator", "orchestrator")
    slot(world.ctx("implementer", "s-1"), {"op": "claim"}, None)
    with pytest.raises(Refused, match="releases its own slot"):
        slot(world.ctx(), {"op": "free", "slot": 1}, None)
    sessions = registry.read(world.places)
    sessions["s-1"].state = RETIRED
    registry.write(world.places, sessions)
    world.launcher.stack = lambda n, verb: StackResult(0, "", "/log")
    assert slot(world.ctx(), {"op": "free", "slot": 1}, None)["released"] is True
    assert world.launcher.stacks == [(1, ["empty"]), (1, [leases.SWEEP])] and leases.read(world.places, 1) is None


def test_one_tester_holds_a_slots_desktop_until_the_warden_or_its_parent_takes_it_down(world: World) -> None:
    world.with_settings(qa__desktop_wait_minutes=0)
    _implementer(world, 1)
    for agent in ("agent-qa-1", "agent-qa-2", "agent-qa-3"):
        world.session(agent, "qa-ux", f"impl-t-0001.{agent}", "t-0001", "/wt/impl-t-0001", agent_id=agent)
    slot(world.ctx("implementer", "s-1"), {"op": "claim"}, None)
    up = {"op": "desktop", "desktop": ["up", "tester-1@example.com"]}

    slot(world.ctx("implementer", "s-1", "agent-qa-1"), up, None)
    with pytest.raises(Refused, match="desktop is impl-t-0001.agent-qa-1's"):
        slot(world.ctx("implementer", "s-1", "agent-qa-2"), up, None)

    sessions = registry.read(world.places)
    sessions["agent-qa-1"].state = RETIRED
    registry.write(world.places, sessions)
    with pytest.raises(Refused, match="going down after impl-t-0001.agent-qa-1 handed back"):
        slot(world.ctx("implementer", "s-1", "agent-qa-2"), up, None)
    reap.close_desktops(world.ctx())
    slot(world.ctx("implementer", "s-1", "agent-qa-2"), up, None)  # the warden took the first's down
    slot(world.ctx("implementer", "s-1"), {"op": "desktop", "desktop": ["down"]}, None)
    slot(world.ctx("implementer", "s-1", "agent-qa-3"), up, None)  # its parent took it down
    assert [verb[:2] for _, verb in world.launcher.stacks] == [
        ["desktop", "up"], ["desktop", "down"], ["desktop", "up"], ["desktop", "down"], ["desktop", "up"]
    ]  # fmt: skip


def test_the_machine_runs_at_most_its_cap_of_desktops_and_a_finished_testers_counts_for_nothing(world: World) -> None:
    world.with_settings(slots__count=3, caps__desktops_at_once=2, qa__desktop_wait_minutes=0)
    for n in (1, 2, 3):
        _implementer(world, n)
        world.session(f"agent-qa-{n}", "qa-ux", f"impl-t-000{n}.qa", f"t-000{n}", f"/wt/impl-t-000{n}",
                      agent_id=f"agent-qa-{n}")  # fmt: skip
        slot(world.ctx("implementer", f"s-{n}"), {"op": "claim"}, None)
    up = {"op": "desktop", "desktop": ["up", "tester-1@example.com"]}
    slot(world.ctx("implementer", "s-1", "agent-qa-1"), up, None)
    slot(world.ctx("implementer", "s-2", "agent-qa-2"), up, None)
    holders = "impl-t-0001.qa's on slot 1, impl-t-0002.qa's on slot 2"
    with pytest.raises(Refused, match=f"2 desktops are up on this machine, its cap of 2: {holders};"):
        slot(world.ctx("implementer", "s-3", "agent-qa-3"), up, None)

    sessions = registry.read(world.places)
    sessions["agent-qa-1"].state = RETIRED
    registry.write(world.places, sessions)
    slot(world.ctx("implementer", "s-3", "agent-qa-3"), up, None)
    assert [n for n, _ in world.launcher.stacks] == [1, 2, 3]


@pytest.mark.parametrize("frees", [True, False], ids=["its-holder-hands-back", "none-frees-in-time"])
def test_a_testers_desktop_up_waits_for_a_desktop_to_free_then_takes_it_or_is_refused(
    world: World, monkeypatch: pytest.MonkeyPatch, frees: bool
) -> None:
    world.with_settings(slots__count=2, caps__desktops_at_once=1, qa__desktop_wait_minutes=5)
    for n in (1, 2):
        _implementer(world, n)
        world.session(f"agent-qa-{n}", "qa-ux", f"impl-t-000{n}.qa", f"t-000{n}", f"/wt/impl-t-000{n}",
                      agent_id=f"agent-qa-{n}")  # fmt: skip
        slot(world.ctx("implementer", f"s-{n}"), {"op": "claim"}, None)
    up = {"op": "desktop", "desktop": ["up", "tester-1@example.com"]}
    slot(world.ctx("implementer", "s-1", "agent-qa-1"), up, None)

    ticks: list[float] = []

    def tick(seconds: float) -> None:
        ticks.append(seconds)
        world.clock.at += 60
        if frees and len(ticks) == 2:  # its holder hands back two minutes in
            sessions = registry.read(world.places)
            sessions["agent-qa-1"].state = RETIRED
            registry.write(world.places, sessions)

    monkeypatch.setattr(slots_verbs.time, "sleep", tick)
    if frees:
        slot(world.ctx("implementer", "s-2", "agent-qa-2"), up, None)
        assert [n for n, _ in world.launcher.stacks] == [1, 2]
    else:
        with pytest.raises(Refused, match="1 desktops are up on this machine, its cap of 1"):
            slot(world.ctx("implementer", "s-2", "agent-qa-2"), up, None)
        assert [n for n, _ in world.launcher.stacks] == [1]


@pytest.mark.parametrize("hands_back", [True, False], ids=["the-other-hands-back", "at-the-deadline"])
def test_a_testers_desktop_up_records_its_desktop_then_waits_for_the_other_browser_walks(
    world: World, monkeypatch: pytest.MonkeyPatch, hands_back: bool
) -> None:
    world.with_settings(slots__count=2, qa__desktop_wait_minutes=5)
    for n, kind in ((1, "qa-ux"), (2, "qa-ui-function")):
        _implementer(world, n)
        world.session(f"agent-qa-{n}", kind, f"impl-t-000{n}.qa", f"t-000{n}", f"/wt/impl-t-000{n}",
                      agent_id=f"agent-qa-{n}", parent=f"s-{n}")  # fmt: skip
        slot(world.ctx("implementer", f"s-{n}"), {"op": "claim"}, None)

    ticks: list[float] = []

    def tick(seconds: float) -> None:
        assert read_desktops(world.places) == {"2": "agent-qa-2"} and world.launcher.stacks == []
        ticks.append(seconds)
        world.clock.at += 60
        if hands_back and len(ticks) == 2:
            sessions = registry.read(world.places)
            sessions["agent-qa-1"].state = RETIRED
            registry.write(world.places, sessions)

    monkeypatch.setattr(slots_verbs.time, "sleep", tick)
    slot(world.ctx("implementer", "s-2", "agent-qa-2"), {"op": "desktop", "desktop": ["up", "t@example.com"]}, None)
    assert len(ticks) == (2 if hands_back else 5)
    assert world.launcher.stacks == [(2, ["desktop", "up", "t@example.com"])]


def _desktop_slot(world: World) -> dict[str, object]:
    world.with_settings(budget__walk_hold_percent=65.0, budget__walk_hold_minutes=15, budget__cpu_window_seconds=300)
    _implementer(world, 1)
    world.session("agent-qa-1", "qa-ux", "impl-t-0001.qa", "t-0001", "/wt/impl-t-0001", agent_id="agent-qa-1")
    slot(world.ctx("implementer", "s-1"), {"op": "claim"}, None)
    return {"op": "desktop", "desktop": ["up", "tester-1@example.com"]}


def _cpu(world: World, cpu: float | None, age: int = 0) -> None:
    at = datetime.fromtimestamp(world.clock.now() - age, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    public.write_vitals(world.places, "green", "", cpu, at)


@pytest.mark.parametrize(
    ("cpu", "age", "off", "said"),
    [
        (50.0, 0, False, None),
        (80.0, 0, False, "averages 80%, past the 65%"),
        (None, 0, False, "unknown"),
        (50.0, 301, False, "unknown"),
        (80.0, 0, True, None),
    ],
    ids=["under", "past-the-hold", "unread", "stale", "switched-off"],
)
def test_a_sessions_own_desktop_waits_while_the_machines_cpu_use_is_past_the_hold_or_unknown(
    world: World, cpu: float | None, age: int, off: bool, said: str | None
) -> None:
    up = _desktop_slot(world)
    _cpu(world, cpu, age)
    public.write_switches(world.places, ["cpu-hold"] if off else [])
    implementer = world.ctx("implementer", "s-1")
    if said is None:
        slot(implementer, up, None)
        assert world.launcher.stacks == [(1, ["desktop", "up", "tester-1@example.com"])]
        return
    with pytest.raises(Refused, match=f"{said}.*wait or do other work, then try again") as refused:
        slot(implementer, up, None)
    assert refused.value.rule == "cpu-hold" and world.launcher.stacks == []


def test_a_testers_desktop_up_answers_with_its_desktop_time_and_a_sessions_own_with_none(world: World) -> None:
    up = _desktop_slot(world)
    world.with_settings(qa__desktop_seconds=420)
    _cpu(world, 20.0)
    assert slot(world.ctx("implementer", "s-1", "agent-qa-1"), up, None)["desktop_seconds"] == 420
    assert "desktop_seconds" not in slot(world.ctx("implementer", "s-1"), up, None)


def test_a_testers_desktop_is_part_of_the_walk_the_hold_let_start_so_it_never_waits(world: World) -> None:
    up = _desktop_slot(world)
    _cpu(world, None)
    slot(world.ctx("implementer", "s-1", "agent-qa-1"), up, None)
    assert world.launcher.stacks == [(1, ["desktop", "up", "tester-1@example.com"])]


def test_a_held_desktop_goes_up_once_its_slot_has_waited_the_longest_hold(world: World) -> None:
    up = _desktop_slot(world)
    _cpu(world, 80.0)
    with pytest.raises(Refused, match="or anyway in 15 min"):
        slot(world.ctx("implementer", "s-1"), up, None)
    world.clock.at += 14 * 60
    _cpu(world, 80.0)
    with pytest.raises(Refused, match="or anyway in 1 min; wait or do other work"):
        slot(world.ctx("implementer", "s-1"), up, None)
    world.clock.at += 60
    _cpu(world, 80.0)
    slot(world.ctx("implementer", "s-1"), up, None)
    with pytest.raises(Refused, match="or anyway in 15 min"):
        slot(world.ctx("implementer", "s-1"), up, None)  # the wait starts again once the desktop went up
    assert world.launcher.stacks == [(1, ["desktop", "up", "tester-1@example.com"])]
