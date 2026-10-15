from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
from warden_fakes import OWNER_CALLER, FakeClock, FakeGitHub, FakeLauncher, defaults_text

from runtime.shared import settings, totp
from runtime.shared.paths import Places
from runtime.warden.context import CheckPlaces, Context
from runtime.warden.dispatch import Doors


@pytest.fixture
def places(tmp_path: Path) -> Places:
    places = Places.under(tmp_path)
    places.install.mkdir(parents=True)
    places.defaults.write_text(defaults_text())
    places.state.mkdir(parents=True)
    places.totp_secret.write_text(totp.new_secret())
    return places


@pytest.fixture
def launcher() -> FakeLauncher:
    return FakeLauncher()


@pytest.fixture
def doors(places: Places, launcher: FakeLauncher) -> Doors:
    events: list[tuple[str, dict[str, Any]]] = []

    def fresh() -> settings.Settings:
        return settings.load(places.defaults, places.overrides)

    return Doors(places, fresh, FakeClock(), launcher, FakeGitHub(),
                 lambda kind, data: events.append((kind, data)), threading.RLock(), CheckPlaces())  # fmt: skip


@pytest.fixture
def ctx(doors: Doors) -> Context:
    return doors.context(OWNER_CALLER)
