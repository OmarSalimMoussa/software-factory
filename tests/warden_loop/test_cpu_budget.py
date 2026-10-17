"""The machine's CPU budget: its average use counts what the CPUs did and not what the host took back, and the
factory's slice gets what the budget leaves after everything else, never less than its floor."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from loop_fakes import World

from runtime.shared import store
from runtime.warden.health import cpu_budget, vitals

HZ = os.sysconf("SC_CLK_TCK")


def _counters(tmp_path: Path, busy: int, idle: int, steal: int, slice_seconds: float) -> tuple[Path, Path]:
    proc, cgroups = tmp_path / "proc", tmp_path / "cgroup"
    proc.mkdir(exist_ok=True)
    line = f"{busy} 0 0 {idle} 0 0 0 {steal} 0 0"
    (proc / "stat").write_text(f"cpu  {line}\ncpu0 {line}\nintr 0\n")
    cpu_budget.slice_dir(cgroups).mkdir(parents=True, exist_ok=True)
    (cpu_budget.slice_dir(cgroups) / "cpu.stat").write_text(f"usage_usec {round(slice_seconds * 1e6)}\nuser_usec 0\n")
    return proc, cgroups


def test_the_average_counts_what_the_cpus_did_and_not_what_the_host_took_back(world: World, tmp_path: Path) -> None:
    settings = world.ctx.settings()
    # Since the machine started: 100 s on one CPU, 40 s busy, 30 s taken back by the host, 10 s of it the factory's.
    first = cpu_budget.update(world.ctx.places, *_counters(tmp_path, 40 * HZ, 30 * HZ, 30 * HZ, 10), settings)
    assert (first["use"], first["outside"]) == pytest.approx((0.40, 0.30))
    # Ten seconds all busy outside the factory: the average moves toward that by ten seconds against its window.
    later = cpu_budget.update(world.ctx.places, *_counters(tmp_path, 50 * HZ, 30 * HZ, 30 * HZ, 10), settings)
    assert 0.40 < later["use"] < 0.50 and 0.30 < later["outside"] < 0.40
    # The machine started again: the counters went back, and the average starts again from them.
    again = cpu_budget.update(world.ctx.places, *_counters(tmp_path, 1 * HZ, 9 * HZ, 0, 0), settings)
    assert again["use"] == pytest.approx(0.10)


@pytest.mark.parametrize(
    ("outside", "quota"),
    [(0.0, 220), (0.64, 156), (2.5, 60)],
    ids=["idle-machine", "prod-and-the-slots-busy", "never-under-the-floor"],
)
def test_the_factory_gets_what_the_budget_leaves_after_everything_else(
    world: World, outside: float, quota: int
) -> None:
    # The budget is 55% of four CPUs, 2.2 of them; the factory never keeps less than 15%, 0.6 of them.
    store.write_json(world.ctx.places.cpu, {"cpus": 4, "use": 0.5, "outside": outside})
    cpu_budget.apply(world.ctx)
    cpu_budget.apply(world.ctx)  # the same quota is not set again
    assert world.launcher.slices == [([f"CPUQuota={quota}%"], True)]


def test_unreadable_counters_leave_the_cpu_vital_unknown_and_the_quota_as_it_was(world: World, tmp_path: Path) -> None:
    proc, _ = _counters(tmp_path, 40 * HZ, 60 * HZ, 0, 10)
    snapshot = vitals.refresh(world.ctx, proc, tmp_path / "no-power", tmp_path / "no-cgroup")
    cpu = next(r for r in snapshot["readings"] if r["name"] == "cpu")
    assert cpu["level"] == vitals.UNKNOWN and "cannot read the CPU counters" in cpu["value"]
    cpu_budget.apply(world.ctx)
    assert world.launcher.slices == []
