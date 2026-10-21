from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from dashboard_rig import Rig
from dashboard_seed import T0, seed
from fake_warden import FakeWarden, FixedClock

from runtime.dashboard.client import Warden
from runtime.dashboard.server import App, PublicSettings


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    places = seed(tmp_path)
    clock = FixedClock(T0)
    warden = FakeWarden(places, clock).start()
    app = App(PublicSettings(places), Warden(warden.address, PublicSettings(places)), clock.now)
    try:
        yield Rig(places, clock, warden, app)
    finally:
        warden.stop()
