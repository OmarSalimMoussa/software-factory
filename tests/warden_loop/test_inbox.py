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


def test_an_event_is_handed_over_once(world: World) -> None:
    inbox.post_event(world.ctx.places, world.clock, inbox.LOOP_DUE, {})
    inbox.post_event(world.ctx.places, world.clock, inbox.TRIAGE_DUE, {"waiting": 100})
    assert [e["kind"] for e in inbox.read(world.ctx, {}, None)["events"]] == [inbox.LOOP_DUE, inbox.TRIAGE_DUE]
    assert inbox.read(world.ctx, {}, None)["events"] == []
    inbox.post_event(world.ctx.places, world.clock, inbox.LOOP_DUE, {})
    assert [e["seq"] for e in inbox.read(world.ctx, {}, None)["events"]] == [3]
