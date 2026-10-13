from __future__ import annotations

import shutil
import tomllib
from pathlib import Path

import pytest
from lifecycle_fakes import ROOT, FakeClock, FakeLauncher, Queue, World

from runtime.shared import settings as settings_mod
from runtime.shared.paths import Places
from runtime.warden.owner import settings_save
from runtime.warden.sessions import backstop, launch, permits
from runtime.warden.sessions.backstop import Proc


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    places = Places.under(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    goal = [
        "- G1 Sign in works. Done when it does.",
        "- G2 Pages load. Done when they do.",
        "- G3 Export. Done when it is.",
    ]
    (repo / "GOAL.md").write_text("# Goal\n\n## The goal\n\n" + "\n".join(goal) + "\n")
    defaults = tomllib.loads((ROOT / "settings.toml").read_text())
    overrides = {"repo.path": str(repo), "repo.github": "acme/product", "caps.implementers": 1, "caps.designers": 1}
    procs: dict[str, Proc] = {}
    built = World(
        places, settings_mod.build(defaults, overrides), FakeClock(), FakeLauncher(procs, places), Queue(), procs, []
    )
    built.vitals("green")
    for part in ("plugin", "procedures", "rules"):
        shutil.copytree(ROOT / part, places.install / part)
    settings_save.render_plugin(places, built.settings)
    monkeypatch.setattr(permits, "read_task", built.queue.read)
    monkeypatch.setattr(permits, "move_task", built.queue.move)
    monkeypatch.setattr(backstop, "processes", lambda: dict(built.procs))
    built.daemon = built.clock.now() - 86400
    monkeypatch.setattr(backstop, "daemon_started", lambda: built.daemon)
    monkeypatch.setattr(launch, "agent_home", lambda: tmp_path / "agent")
    return built
