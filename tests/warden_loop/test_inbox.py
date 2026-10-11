from __future__ import annotations

import threading
import time
from typing import Any

from loop_fakes import World

from runtime.warden import inbox


def test_wait_returns_as_soon_as_an_event_arrives(world: World) -> None:
    got: dict[str, Any] = {}
    waiter = threading.Thread(target=lambda: got.update(inbox.wait(world.ctx, {}, None)))
    started = time.monotonic()
    waiter.start()
    time.sleep(0.2)
    inbox.post_event(world.ctx.places, world.clock, inbox.PROD_RED, {"since": "now"})
    waiter.join(timeout=10)
    assert not waiter.is_alive()
    assert time.monotonic() - started < world.ctx.settings().get_int("watch.inbox_wait_seconds")
    assert [e["kind"] for e in got["events"]] == [inbox.PROD_RED]


def _kinds(answer: dict[str, Any]) -> list[str]:
    return [e["kind"] for e in answer["events"]]


def test_an_event_is_handed_until_the_next_wait_says_it_was_acted_on(world: World) -> None:
    short = world.ctx.settings().with_changes({"watch.inbox_wait_seconds": 5})
    world.ctx.settings = lambda: short
    post = world.ctx.post_event = inbox.poster(world.ctx.places, world.clock)
    post(inbox.LOOP_DUE, {})
    post(inbox.TRIAGE_DUE, {"waiting": 100})

    assert _kinds(inbox.wait(world.ctx, {}, None)) == [inbox.LOOP_DUE, inbox.TRIAGE_DUE]
    # The orchestrator restarted before it acted: nothing it was handed is lost.
    assert _kinds(inbox.read(world.ctx, {}, None)) == [inbox.LOOP_DUE, inbox.TRIAGE_DUE]
    assert inbox.pending(world.ctx.places, inbox.TRIAGE_DUE)

    post(inbox.PROD_RED, {})
    assert _kinds(inbox.wait(world.ctx, {}, None)) == [inbox.PROD_RED]  # the new one only; the rest acted on
    assert not inbox.pending(world.ctx.places, inbox.TRIAGE_DUE)
    assert _kinds(inbox.read(world.ctx, {}, None)) == [inbox.PROD_RED]
