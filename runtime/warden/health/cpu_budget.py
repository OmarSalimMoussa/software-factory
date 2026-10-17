"""The machine's CPU budget, for a host that meters CPU on credits: use above its baseline share of the CPUs spends
credits, and once they are gone the host holds the whole machine far below it, prod included.

Every tick the warden reads the machine's CPU counters and the factory slice's, and keeps two running averages in
cpu.json: the machine's use, as a share of all its CPUs, and the use outside the slice (prod, the shared services, the
slots' containers, image builds), in CPUs. Time the host took back (steal) is not use. Above
`budget.cpu_target_percent` the CPU vital is amber, so new work waits. The slice's CPU quota is the target less the use
outside it, never under `budget.cpu_floor_percent`, so the machine's use settles at the target however much work the
factory has.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runtime.shared import store
from runtime.shared.paths import SLICE, Places
from runtime.shared.settings import Settings
from runtime.warden.context import Context

CGROUPS = Path("/sys/fs/cgroup")
_CPU_LINE = re.compile(r"^cpu\d+\s", re.MULTILINE)
_USEC = 1_000_000
_HZ = os.sysconf("SC_CLK_TCK")
# By the warden's state folder, the quota last set, so systemd is asked again only for a change.
_APPLIED: dict[str, int] = {}


class BudgetError(Exception):
    """The CPU counters cannot be read."""


@dataclass(frozen=True, slots=True)
class Sample:
    cpus: int
    busy: int
    total: int
    slice_usec: int


def slice_dir(cgroups: Path) -> Path:
    # systemd nests a-b.slice inside a.slice.
    parts = SLICE.removesuffix(".slice").split("-")
    return cgroups.joinpath(*("-".join(parts[: n + 1]) + ".slice" for n in range(len(parts))))


def sample(proc: Path, cgroups: Path) -> Sample:
    try:
        text = (proc / "stat").read_text()
        user, nice, system, idle, iowait, irq, softirq, steal = (int(v) for v in text.split("\n", 1)[0].split()[1:9])
        stat = dict(line.split() for line in (slice_dir(cgroups) / "cpu.stat").read_text().splitlines())
        inside = int(stat["usage_usec"])
    except (OSError, ValueError, KeyError) as exc:
        raise BudgetError(f"cannot read the CPU counters: {type(exc).__name__}: {exc}") from exc
    busy = user + nice + system + irq + softirq
    return Sample(len(_CPU_LINE.findall(text)), busy, busy + idle + iowait + steal, inside)


def advance(before: dict[str, Any] | None, now: Sample, window: float) -> dict[str, Any]:
    """The averages after ``now``; the first sample, or one after a restart, starts from the counters since boot."""
    counters = {"cpus": now.cpus, "busy": now.busy, "total": now.total, "slice_usec": now.slice_usec}
    if before is None or now.total <= before["total"]:
        before = {"busy": 0, "total": 0, "slice_usec": 0, "use": None, "outside": None}
    ticks = now.total - before["total"]
    seconds = ticks / (now.cpus * _HZ)
    use = (now.busy - before["busy"]) / ticks
    outside = max(0.0, use * now.cpus - (now.slice_usec - before["slice_usec"]) / _USEC / seconds)
    weight = 1 - math.exp(-seconds / window)

    def blend(old: float | None, new: float) -> float:
        return new if old is None else old + weight * (new - old)

    return {**counters, "use": blend(before["use"], use), "outside": blend(before["outside"], outside)}


def update(places: Places, proc: Path, cgroups: Path, settings: Settings) -> dict[str, Any]:
    window = settings.get_int("budget.cpu_window_seconds")
    after = advance(store.read_json_or(places.cpu, None), sample(proc, cgroups), window)
    store.write_json(places.cpu, after)
    return after


def quota(outside: float, cpus: int, settings: Settings) -> int:
    target = settings.get_float("budget.cpu_target_percent") * cpus
    floor = settings.get_float("budget.cpu_floor_percent") * cpus
    return round(max(floor, target - outside * 100))


def apply(ctx: Context) -> None:
    state = store.read_json_or(ctx.places.cpu, None)
    if state is None:  # no reading yet: the vitals step reports why
        return
    settings = ctx.settings()
    percent = quota(state["outside"], state["cpus"], settings)
    key = str(ctx.places.state)
    if _APPLIED.get(key) != percent:
        ctx.launcher.set_slice([f"CPUQuota={percent}%"], runtime=True)
        _APPLIED[key] = percent
