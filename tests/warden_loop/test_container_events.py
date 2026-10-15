"""Slot containers that stopped when nothing asked them to: reported once each, from Docker's events."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from loop_fakes import World

from runtime.slots import leases
from runtime.warden.context import StackResult
from runtime.warden.health import container_events


def _event(action: str, at: int, cid: str = "c1", exit_code: str = "") -> dict[str, Any]:
    return {"action": action, "at": str(at), "id": cid, "name": f"slot2-{cid}", "slot": "2", "exit": exit_code}


@pytest.mark.parametrize(
    ("events", "reported"),
    [
        ([_event("kill", 1), _event("die", 2, exit_code="137")], []),
        ([_event("die", 2, exit_code="137")], ["c1"]),
        ([_event("oom", 1), _event("die", 2, exit_code="137")], ["c1"]),
        ([_event("die", 2, exit_code="0")], []),
        ([_event("die", 3, "c2", "139"), _event("die", 2, "c1", "1")], ["c1", "c2"]),
    ],
    ids=["the-factorys-own-stop", "killed-from-outside", "out-of-memory-once", "a-clean-exit", "two-in-order"],
)
def test_only_a_stop_nothing_asked_for_is_reported_once_per_container(
    events: list[dict[str, Any]], reported: list[str]
) -> None:
    assert [e["id"] for e in container_events.unasked(events)] == reported


class _Events:
    def __init__(self, logs: Path, lines: list[dict[str, Any]]) -> None:
        self.logs, self.lines = logs, lines
        self.calls: list[list[str]] = []

    def run_stack(self, slot: int, verb: list[str], session: str, given: str = "") -> StackResult:
        self.calls.append(verb)
        log = self.logs / f"events-{len(self.calls)}.log"
        log.write_text("".join(json.dumps(line) + "\n" for line in self.lines))
        return StackResult(0, "", str(log))


def test_an_unasked_stop_since_the_last_read_is_logged_as_an_error(
    world: World, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    stack = _Events(tmp_path, [_event("oom", 1)])
    world.ctx.launcher = stack  # type: ignore[assignment]  # the watch runs only the stack unit's events verb
    start = int(world.clock.t)

    assert container_events.run_pass(world.ctx) == 0  # the first read only starts the span
    world.clock.t += 60
    assert container_events.run_pass(world.ctx) == 0 and stack.calls == []
    world.clock.t += 5 * 60
    with caplog.at_level(logging.ERROR, logger="factory.warden"):
        assert container_events.run_pass(world.ctx) == 1

    assert stack.calls == [[leases.EVENTS, str(start), str(int(world.clock.t))]]
    assert "slot 2's container slot2-c1 ran out of memory" in caplog.text
