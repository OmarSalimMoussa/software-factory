"""The laptop's vitals as the permits read them: a wall that stopped loading blocks every start."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from loop_fakes import World

from runtime.warden import vitals


@pytest.mark.parametrize(
    ("age", "level"),
    [(10, vitals.GREEN), (179, vitals.GREEN), (181, vitals.RED), (None, vitals.RED)],
    ids=["fresh", "just-in-time", "stale", "never-loaded"],
)
def test_the_firewall_is_red_once_it_stops_loading(world: World, tmp_path: Path, age: int | None, level: str) -> None:
    stamp = tmp_path / "firewall-loaded"
    if age is not None:
        stamp.touch()
        at = world.clock.now() - age
        os.utime(stamp, (at, at))
    reading = vitals.firewall(stamp, world.clock.now(), world.ctx.settings())
    assert reading.level == level


def _supply(root: Path, name: str, **files: str) -> None:
    (root / name).mkdir(parents=True)
    for key, text in files.items():
        (root / name / key).write_text(text + "\n")


@pytest.mark.parametrize(
    ("supplies", "level"),
    [
        ({}, vitals.GREEN),  # a desktop or a VM lists no supply
        ({"hidpp_battery_0": {"type": "Battery", "scope": "Device", "capacity": "5"}}, vitals.GREEN),
        ({"AC": {"type": "Mains", "online": "1"}, "BAT0": {"type": "Battery", "capacity": "50"}}, vitals.GREEN),
        ({"AC": {"type": "Mains", "online": "0"}, "BAT0": {"type": "Battery", "capacity": "50"}}, vitals.AMBER),
        ({"BAT0": {"type": "Battery", "capacity": "5"}}, vitals.RED),
    ],
    ids=["none-listed", "a-mouse-battery", "on-mains", "on-battery", "battery-low"],
)
def test_a_machine_with_no_battery_of_its_own_is_on_mains(
    world: World, tmp_path: Path, supplies: dict[str, dict[str, str]], level: str
) -> None:
    root = tmp_path / "power_supply"
    root.mkdir()
    for name, files in supplies.items():
        _supply(root, name, **files)
    assert vitals.power(root, world.ctx.settings()).level == level


def test_a_reading_the_kernel_does_not_have_is_missing_and_blocks_nothing(world: World) -> None:
    missing = vitals.cpu(None, world.ctx.settings())
    green = vitals.Reading("memory", vitals.GREEN, "8.0 GiB free")
    assert missing.level == vitals.MISSING
    assert vitals.worst([green, missing]) == vitals.GREEN
    assert vitals.worst([green, vitals.cpu("", world.ctx.settings())]) == vitals.UNKNOWN  # unreadable is not missing
