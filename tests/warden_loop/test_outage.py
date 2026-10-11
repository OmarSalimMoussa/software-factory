from __future__ import annotations

import pytest
from loop_fakes import World, answers, healthy

from runtime.shared.protocol import Refused
from runtime.warden.delivery import deploys, outage
from runtime.warden.health import probes
from runtime.warden.health.probes import Answer, NetworkFailure

PROD_RED = "prod red"


def probe_prod(world: World, *replies: Answer | Exception) -> None:
    fetch = answers(*replies)
    for _ in replies:
        world.clock.t += 60
        outage.observe(world.ctx, probes.take(world.ctx, probes.PROD, fetch))


@pytest.mark.parametrize(
    "failure",
    [Answer(502, b""), TimeoutError("slow"), Answer(200, b'{"status": "ok"}'), Answer(302, b"")],
    ids=["http-error", "timeout", "no-build-commit", "redirect"],
)
def test_two_counted_failures_in_a_row_declare_one_outage(world: World, failure: Answer | Exception) -> None:
    probe_prod(world, failure)
    assert outage.read(world.ctx.places) is None
    probe_prod(world, failure, failure)
    current = outage.read(world.ctx.places)
    assert current is not None and current.stage == outage.DOWN
    # The warden files the outage's severe request itself, and names it in the event.
    assert [kind for kind, _ in world.events] == ["severe request", PROD_RED]
    assert world.events[1][1]["request"] == world.events[0][1]["request"]
    probe_prod(world, healthy("a" * 40))
    assert outage.read(world.ctx.places) is None


def test_a_prod_failure_counts_only_when_the_control_probe_reaches_github(world: World) -> None:
    world.github.up = False
    probe_prod(world, Answer(503, b""), Answer(503, b""), TimeoutError("slow"))
    assert outage.read(world.ctx.places) is None
    assert not any(p.counts for p in probes.recent(world.ctx.places, probes.PROD, 10))
    assert world.events == []


@pytest.mark.parametrize(
    "replies",
    [
        (NetworkFailure("gaierror"), NetworkFailure("ConnectionRefusedError"), NetworkFailure("SSLError")),
        (Answer(502, b""), NetworkFailure("gaierror"), Answer(502, b"")),
    ],
    ids=["only-network", "network-between-http-errors"],
)
def test_a_network_failure_on_this_side_never_counts(world: World, replies: tuple[Answer | Exception, ...]) -> None:
    probe_prod(world, *replies)
    assert outage.read(world.ctx.places) is None
    assert world.events == []


def test_an_outage_allows_one_bypass_run_only(world: World) -> None:
    probe_prod(world, Answer(500, b""), Answer(500, b""))
    assert outage.bypass_allowed(world.ctx)
    outage.mark_bypass(world.ctx, "t-0001", "s-1")
    current = outage.read(world.ctx.places)
    assert current is not None and (current.stage, current.bypass_used) == (outage.FIXING, True)
    assert not outage.bypass_allowed(world.ctx)
    with pytest.raises(Refused):
        outage.mark_bypass(world.ctx, "t-0002", "s-2")
    probe_prod(world, Answer(500, b""))  # still down: the same outage, still no second run
    assert not outage.bypass_allowed(world.ctx)


def test_the_owners_push_restarting_prod_is_no_outage_until_prod_answers_on_the_new_build(world: World) -> None:
    deploys.expect(world.ctx.places, probes.PROD, "b" * 40, world.clock.now(), 30)
    probe_prod(world, Answer(502, b""), Answer(502, b""), Answer(502, b""))  # the containers restart
    assert outage.read(world.ctx.places) is None and world.events == []

    probe_prod(world, healthy("b" * 40))  # it landed: from now on a failure counts
    probe_prod(world, Answer(502, b""), Answer(502, b""))
    current = outage.read(world.ctx.places)
    assert current is not None and current.stage == outage.DOWN
