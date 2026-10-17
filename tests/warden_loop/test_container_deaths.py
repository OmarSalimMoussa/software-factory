"""Containers that stopped when nothing asked them to: each rise of Docker's restart count reported once."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from loop_fakes import World

from runtime.slots import leases
from runtime.warden.context import StackResult
from runtime.warden.health import container_deaths, probes
from standard.services import stack


def _container(cid: str, restarts: int, slot: str = "", project: str = "") -> dict[str, Any]:
    return {"id": cid, "name": f"{project or 'loose'}-{cid}", "restarts": restarts, "slot": slot, "project": project}


@pytest.mark.parametrize(
    ("container", "home"),
    [
        (_container("c1", 0, slot="2", project="notes-slot2"), "slot 2"),
        (_container("c2", 0, project="notes-prod"), "prod"),
        (_container("c3", 0, project=stack.PROJECT), "shared services"),
        (_container("c4", 0, project="another-app"), ""),
        (_container("c5", 0), ""),
    ],
    ids=["a-slot", "a-workspace", "the-shared-services", "another-project", "no-project"],
)
def test_only_the_factorys_containers_are_watched(world: World, container: dict[str, Any], home: str) -> None:
    assert container_deaths.whose(probes.product(world.ctx), container) == home


class _Counts:
    def __init__(self, logs: Path) -> None:
        self.logs, self.exit = logs, 0
        self.lines: list[dict[str, Any]] = []
        self.calls: list[list[str]] = []

    def run_stack(self, slot: int, verb: list[str], session: str, given: str = "") -> StackResult:
        self.calls.append(verb)
        log = self.logs / f"restarts-{len(self.calls)}.log"
        log.write_text("".join(json.dumps(line) + "\n" for line in self.lines))
        return StackResult(self.exit, "", str(log))


def test_a_count_that_rose_since_the_last_list_is_logged_as_an_error(
    world: World, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    stack_unit = _Counts(tmp_path)
    world.ctx.launcher = stack_unit  # type: ignore[assignment]  # the watch runs only the stack unit's restarts verb
    stack_unit.lines = [
        _container("c1", 1, slot="2", project="notes-slot2"),
        _container("c3", 4, project=stack.PROJECT),
        _container("c4", 0, project="another-app"),
    ]

    assert container_deaths.run_pass(world.ctx) == 0  # the first list only starts the count
    world.clock.t += 60
    assert container_deaths.run_pass(world.ctx) == 0 and len(stack_unit.calls) == 1
    world.clock.t += 5 * 60
    stack_unit.lines = [
        _container("c1", 3, slot="2", project="notes-slot2"),
        _container("c2", 1, project="notes-prod"),  # new since the last list, and stopped once since
        _container("c3", 4, project=stack.PROJECT),
        _container("c4", 9, project="another-app"),
    ]
    with caplog.at_level(logging.ERROR, logger="factory.warden"):
        assert container_deaths.run_pass(world.ctx) == 2

    assert stack_unit.calls == [[leases.RESTARTS], [leases.RESTARTS]]
    said = [r.getMessage() for r in caplog.records]
    assert said == [
        "warden: slot 2 container notes-slot2-c1 stopped 2 times when nothing asked; Docker restarted it",
        "warden: prod container notes-prod-c2 stopped once when nothing asked; Docker restarted it",
    ]


def test_a_list_the_stack_unit_could_not_make_fails_the_pass(world: World, tmp_path: Path) -> None:
    stack_unit = _Counts(tmp_path)
    stack_unit.lines, stack_unit.exit = [], 1
    world.ctx.launcher = stack_unit  # type: ignore[assignment]  # the watch runs only the stack unit's restarts verb

    with pytest.raises(container_deaths.CountsError, match="could not list the containers' restart counts"):
        container_deaths.run_pass(world.ctx)
