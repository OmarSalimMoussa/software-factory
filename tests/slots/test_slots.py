"""Stack slots: a lease per implementer, worked out from the caller and never from an argument."""

from __future__ import annotations

import pytest
from queue_kit import World

from runtime.shared import store
from runtime.shared.models import RETIRED
from runtime.shared.protocol import Refused
from runtime.slots import leases
from runtime.slots.verbs import slot
from runtime.warden.context import StackResult
from runtime.warden.sessions import registry


def _implementer(world: World, n: int) -> None:
    world.session(f"s-{n}", "implementer", f"impl-t-000{n}", f"t-000{n}", f"/wt/impl-t-000{n}")


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
    ],
)
def test_only_checked_words_reach_scripts_dev(world: World, args: dict[str, object], agent_id: str) -> None:
    _implementer(world, 1)
    slot(world.ctx("implementer", "s-1"), {"op": "claim"}, None)
    with pytest.raises(Refused):
        slot(world.ctx("implementer", "s-1", agent_id), args, None)
    assert world.launcher.stacks == []


def test_the_holder_restarts_the_services_it_names(world: World) -> None:
    _implementer(world, 1)
    slot(world.ctx("implementer", "s-1"), {"op": "claim"}, None)

    slot(world.ctx("implementer", "s-1"), {"op": "restart", "services": ["web", "worker"]}, None)

    assert world.launcher.stacks == [(1, ["restart", "web", "worker"])]


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


def test_one_tester_holds_a_slots_desktop_until_it_hands_back_or_its_parent_takes_it_down(world: World) -> None:
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
    slot(world.ctx("implementer", "s-1", "agent-qa-2"), up, None)  # the first handed back
    slot(world.ctx("implementer", "s-1"), {"op": "desktop", "desktop": ["down"]}, None)
    slot(world.ctx("implementer", "s-1", "agent-qa-3"), up, None)  # its parent took it down
    assert [verb[:2] for _, verb in world.launcher.stacks] == [["desktop", "up"], ["desktop", "up"],
                                                                ["desktop", "down"], ["desktop", "up"]]  # fmt: skip


def test_the_machine_runs_at_most_its_cap_of_desktops_and_a_finished_testers_counts_for_nothing(world: World) -> None:
    world.with_settings(slots__count=3, caps__desktops_at_once=2)
    for n in (1, 2, 3):
        _implementer(world, n)
        world.session(f"agent-qa-{n}", "qa-ux", f"impl-t-000{n}.qa", f"t-000{n}", f"/wt/impl-t-000{n}",
                      agent_id=f"agent-qa-{n}")  # fmt: skip
        slot(world.ctx("implementer", f"s-{n}"), {"op": "claim"}, None)
    up = {"op": "desktop", "desktop": ["up", "tester-1@example.com"]}
    slot(world.ctx("implementer", "s-1", "agent-qa-1"), up, None)
    slot(world.ctx("implementer", "s-2", "agent-qa-2"), up, None)
    with pytest.raises(Refused, match="2 desktops are up on this machine, its cap of 2"):
        slot(world.ctx("implementer", "s-3", "agent-qa-3"), up, None)

    sessions = registry.read(world.places)
    sessions["agent-qa-1"].state = RETIRED
    registry.write(world.places, sessions)
    slot(world.ctx("implementer", "s-3", "agent-qa-3"), up, None)
    assert [n for n, _ in world.launcher.stacks] == [1, 2, 3]
