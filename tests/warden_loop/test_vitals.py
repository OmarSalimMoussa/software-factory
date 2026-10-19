"""The machine's vitals as the permits read them: a wall that stopped loading blocks every start. The merges waiting for
their check are listed beside them, and never block one."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from loop_fakes import World, kernel

from runtime.guard.facts import read_vitals
from runtime.queue import formats
from runtime.queue.book import Queue
from runtime.shared.models import Task
from runtime.warden import snapshot, status
from runtime.warden.health import vitals
from runtime.warden.sessions import backstop, permits


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
    cpu = vitals.Reading("cpu", vitals.AMBER, "70% of every CPU in use on average")
    assert vitals.why([memory]) == ""
    assert vitals.why([memory, power, cpu]) == "power: on battery, 25%; cpu: 70% of every CPU in use on average"
    assert vitals.why([power, vitals.Reading("install", vitals.RED, "differs")]) == "install: differs"


FAILED = "the last clean-up failed: the Docker prune failed (exit 1)"


@pytest.mark.parametrize(
    ("free_gib", "cleanup", "level", "said"),
    [
        ((120, 31), None, vitals.GREEN, "31 GiB free of 193 GiB"),
        ((120, 30), None, vitals.GREEN, "30 GiB free of 193 GiB"),
        ((120, 29.5), None, vitals.AMBER, "30 GiB free of 193 GiB"),  # the fullest disk sets it
        ((10, 120), None, vitals.AMBER, "10 GiB free of 193 GiB"),
        ((9.5, 120), None, vitals.RED, "10 GiB free of 193 GiB"),
        ((120, 31), FAILED, vitals.AMBER, f"31 GiB free of 193 GiB; {FAILED}"),
        ((9.5, 120), FAILED, vitals.RED, f"10 GiB free of 193 GiB; {FAILED}"),
        (None, None, vitals.UNKNOWN, "cannot read the free disk space: PermissionError"),
    ],
    ids=["plenty", "at-amber", "under-amber", "at-red", "under-red", "failed", "failed-full", "unreadable"],
)
def test_the_disk_vital_turns_amber_then_red_as_the_fullest_disk_fills_and_amber_when_a_clean_up_failed(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    free_gib: tuple[float, float] | None,
    cleanup: str | None,
    level: str,
    said: str,
) -> None:
    state, work = Path("/state"), Path("/work")

    def statvfs(path: Path) -> os.statvfs_result:
        if free_gib is None:
            raise PermissionError(path)
        free = free_gib[[state, work].index(path)] * 1024**3
        return os.statvfs_result((4096, 4096, 193 * 1024**3 // 4096, 0, int(free) // 4096, 0, 0, 0, 0, 255))

    monkeypatch.setattr(vitals.os, "statvfs", statvfs)
    reading = vitals.disk([state, work], world.ctx.settings(), cleanup)
    assert (reading.level, reading.value) == (level, said)
    assert reading.whole == (None if free_gib is None else 193)


@pytest.mark.parametrize(
    ("use", "level"),
    [(0.30, vitals.GREEN), (0.54, vitals.GREEN), (0.56, vitals.AMBER), (0.91, vitals.RED)],
    ids=["under-budget", "just-under-budget", "past-budget", "past-the-red-line"],
)
def test_the_cpu_vital_turns_amber_past_the_budget_so_new_work_waits_and_red_past_its_red_line(
    world: World, use: float, level: str
) -> None:
    assert vitals.cpu(use, world.ctx.settings()).level == level


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


def _merged(world: World, merges: list[tuple[float, ...]], status_now: str = "merged") -> None:
    """A task at ``status_now`` for each entry of ``merges``: the hours ago it merged, oldest first."""
    folder = world.checkout / "queue" / "tasks"
    folder.mkdir(mode=0o755, exist_ok=True)
    folder.chmod(0o755)
    queue, now = Queue.of(world.ctx), world.clock.now()
    for ages in merges:
        task_id = f"t-{len(list(folder.glob('*.md'))) + 1:04d}"
        task = Task(id=task_id, title="A change", requests=["r-2026-10-09-0001"], goal_item="G1", severity="major",
                    surfaces=["api"], status=status_now, created="2026-10-09T00:00:00Z", body="Change it.", run=1,
                    slices=1)  # fmt: skip
        (folder / f"{task_id}.md").write_text(formats.dump_task(task))
        for age in ages:
            moved = {"from": "turn", "to": "merged", "commit": "a" * 40}
            queue.log(task_id, formats.iso(now - age * 3600), "warden", "merged", "merged", **moved)


@pytest.mark.parametrize(
    ("merges", "breaks", "level", "waiting", "oldest"),
    [
        ([], None, vitals.GREEN, 0, None),
        ([(2,), (1,)], None, vitals.GREEN, 2, 2.0),
        ([(30, 1)], None, vitals.GREEN, 1, 1.0),  # merged again after a revert: it waits by its newest merge
        ([(1,)] * 5, None, vitals.AMBER, 5, 1.0),
        ([(7,)], None, vitals.AMBER, 1, 7.0),
        ([(7,)], "queue", vitals.UNKNOWN, None, None),
        ([(1,)], "task", vitals.UNKNOWN, None, None),
    ],
    ids=["none", "under-both", "merged-again", "at-the-cap", "past-the-hours", "unsafe-queue", "unreadable-task"],
)
def test_the_merges_waiting_turn_amber_at_the_cap_or_past_the_hours_and_unknown_on_a_queue_it_cannot_read(
    world: World,
    tmp_path: Path,
    merges: list[tuple[float, ...]],
    breaks: str | None,
    level: str,
    waiting: int | None,
    oldest: float | None,
) -> None:
    _merged(world, [(50,)], status_now="checked")  # its check passed: it waits no more
    _merged(world, merges)
    if breaks == "queue":
        (world.checkout / "queue").chmod(0o777)
    if breaks == "task":
        (world.checkout / "queue" / "tasks" / "t-0099.md").write_text("not a task\n")

    found = vitals.refresh(world.ctx, *kernel(tmp_path))["merges"]

    assert (found["level"], found["waiting"], found["oldest_hours"]) == (level, waiting, oldest), found["value"]
    assert found["value"].startswith("cannot read the queue: ") == (level == vitals.UNKNOWN)
    assert status.status(world.ctx, {}, None)["vitals"]["merges"] == found
    assert snapshot.snapshot(world.ctx, {"want": ["vitals"]}, None)["state"]["vitals"]["reading"]["merges"] == found


def test_amber_merges_leave_the_machines_level_green_and_starts_still_go(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("disk", "install", "hooks", "daemon", "records", "firewall"):
        monkeypatch.setattr(vitals, name, lambda *args, n=name: vitals.Reading(n, vitals.GREEN, "fine"))
    _merged(world, [(52,)] * 34)

    found = vitals.refresh(world.ctx, *kernel(tmp_path))

    assert found["merges"]["level"] == vitals.AMBER
    assert (found["level"], found["why"]) == (vitals.GREEN, "")
    assert read_vitals(world.ctx.places).level == vitals.GREEN
    assert permits.vitals_level(world.ctx) == (vitals.GREEN, "")
    permits.check_vitals(world.ctx, severe=False)  # no refusal: the work that clears the merges may start
