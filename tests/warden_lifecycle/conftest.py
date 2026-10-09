from __future__ import annotations

import shutil
import tomllib
from pathlib import Path

import pytest
from lifecycle_fakes import ROOT, FakeClock, FakeLauncher, Queue, World

from runtime.shared import settings as settings_mod
from runtime.shared.paths import Places
from runtime.warden import backstop, launch, permits, settings_save
from runtime.warden.backstop import Proc
from runtime.warden.context import Context


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    places = Places.under(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    defaults = tomllib.loads((ROOT / "settings.toml").read_text())
    overrides = {"repo.path": str(repo), "repo.github": "acme/product", "repo.staging_health": "https://s/health",
                 "repo.prod_health": "https://p/health", "repo.owner_login": "owner",
                 "caps.implementers": 1, "caps.designers": 1}  # fmt: skip
    procs: dict[str, Proc] = {}
    built = World(
        places, settings_mod.build(defaults, overrides), FakeClock(), FakeLauncher(procs), Queue(), procs, [], []
    )
    built.vitals("green")
    for part in ("plugin", "procedures", "rules"):
        shutil.copytree(ROOT / part, places.install / part)
    settings_save.render_plugin(places, built.settings)
    free = [1, 2, 3]
    monkeypatch.setattr(permits, "read_task", built.queue.read)
    monkeypatch.setattr(permits, "move_task", built.queue.move)

    def free_slot(ctx: Context, n: int) -> None:
        free.insert(0, n)
        built.slots.append(n)

    monkeypatch.setattr(permits, "claim_slot", lambda ctx, s: free.pop(0))
    monkeypatch.setattr(permits, "free_slot", free_slot)
    monkeypatch.setattr(backstop, "processes", lambda: dict(built.procs))
    monkeypatch.setattr(launch, "agent_home", lambda: tmp_path / "agent")
    return built
