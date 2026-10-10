from __future__ import annotations

import shutil
import tomllib
from pathlib import Path
from typing import Any

import pytest
from loop_fakes import FakeClock, FakeGitHub, World

from runtime.shared import settings as settings_mod
from runtime.shared.paths import Places
from runtime.warden.context import Caller, Context

ROOT = Path(__file__).resolve().parents[2]
FACTORY_TOML = ROOT / "testdata" / "fixture-product" / "factory.toml"


@pytest.fixture
def world(tmp_path: Path) -> World:
    defaults = tomllib.loads((ROOT / "settings.toml").read_text())
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    # The product's factory.toml names the health addresses the probes ask.
    shutil.copy(FACTORY_TOML, checkout / "factory.toml")
    settings = settings_mod.build(defaults, {"repo.path": str(checkout), "repo.github": "owner/name"})
    github, clock = FakeGitHub(), FakeClock()
    events: list[tuple[str, dict[str, Any]]] = []
    ctx = Context(
        places=Places.under(tmp_path),
        settings=lambda: settings,
        caller=Caller("owner", role="owner"),
        clock=clock,
        launcher=None,  # type: ignore[arg-type]  # nothing here launches
        github=github,
        post_event=lambda kind, data: events.append((kind, data)),
    )
    return World(ctx, github, clock, events, checkout)
