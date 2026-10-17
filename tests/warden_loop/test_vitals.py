"""The laptop's vitals as the permits read them: a wall that stopped loading blocks every start."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from loop_fakes import World

from runtime.warden.health import vitals
from runtime.warden.sessions import backstop


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
        ({"AC": {"type": "Mains", "online": "0"}, "BAT0": {"type": "Battery", "capacity": "30"}}, vitals.GREEN),
        ({"BAT0": {"type": "Battery", "capacity": "29"}}, vitals.AMBER),
        ({"BAT0": {"type": "Battery", "capacity": "15"}}, vitals.AMBER),
        ({"BAT0": {"type": "Battery", "capacity": "14"}}, vitals.RED),
    ],
    ids=["none-listed", "a-mouse-battery", "on-mains", "on-battery", "below-amber", "at-red", "below-red"],
)
def test_power_is_green_on_mains_or_a_charged_battery_and_turns_amber_then_red_as_it_drains(
    world: World, tmp_path: Path, supplies: dict[str, dict[str, str]], level: str
) -> None:
    root = tmp_path / "power_supply"
    root.mkdir()
    for name, files in supplies.items():
        _supply(root, name, **files)
    assert vitals.power(root, world.ctx.settings()).level == level


def test_the_machines_level_names_the_readings_that_set_it() -> None:
    memory = vitals.Reading("memory", vitals.GREEN, "8.0 GiB free")
    power = vitals.Reading("power", vitals.AMBER, "on battery, 25%")
    cpu = vitals.Reading("cpu", vitals.AMBER, "pressure 70% over a minute")
    assert vitals.why([memory]) == ""
    assert vitals.why([memory, power, cpu]) == "power: on battery, 25%; cpu: pressure 70% over a minute"
    assert vitals.why([power, vitals.Reading("install", vitals.RED, "differs")]) == "install: differs"


def test_a_reading_the_kernel_does_not_have_is_missing_and_blocks_nothing(world: World) -> None:
    missing = vitals.cpu(None, world.ctx.settings())
    green = vitals.Reading("memory", vitals.GREEN, "8.0 GiB free")
    assert missing.level == vitals.MISSING
    assert vitals.worst([green, missing]) == vitals.GREEN
    assert vitals.worst([green, vitals.cpu("", world.ctx.settings())]) == vitals.UNKNOWN  # unreadable is not missing


def _process(proc: Path, pid: int, argv: list[str], started_ticks: int) -> None:
    (proc / str(pid)).mkdir(parents=True)
    (proc / str(pid) / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    fields = ["S", *["0"] * 18, str(started_ticks), "0", "0"]
    (proc / str(pid) / "stat").write_text(f"{pid} (claude) {' '.join(fields)}\n")


def test_a_claude_session_with_no_record_the_warden_reads_is_red(tmp_path: Path) -> None:
    proc, folder, ticks = tmp_path / "proc", tmp_path / "sessions", os.sysconf("SC_CLK_TCK")
    folder.mkdir()
    proc.mkdir()
    (proc / "uptime").write_text("1000.00 0.00\n")
    old = 100 * ticks  # started 900 s ago
    _process(proc, 101, ["claude", "--bg"], old)  # has its record
    _process(proc, 102, ["claude", "daemon", "run"], old)
    _process(proc, 104, ["claude", "--resume"], 999 * ticks)  # a second old: not settled yet
    (folder / "101.json").write_text(json.dumps({"sessionId": "s-101", "pid": 101, "procStart": str(old)}))
    assert backstop.unseen(folder, proc, os.getuid(), 60) == []

    _process(proc, 105, ["/usr/local/libexec/software-factory/claude", "--bg"], old)

    assert backstop.unseen(folder, proc, os.getuid(), 60) == [105]
