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
