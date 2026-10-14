from __future__ import annotations

import shutil
import tomllib
from pathlib import Path
from typing import Any

import pytest
from loop_fakes import FakeClock, FakeGitHub, FakeStack, World, commit, git, write

from runtime.shared import settings as settings_mod
from runtime.shared.paths import Places
from runtime.warden.context import Caller, Context
from runtime.warden.delivery import github_push

ROOT = Path(__file__).resolve().parents[2]
FACTORY_TOML = ROOT / "testdata" / "fixture-product" / "factory.toml"


@pytest.fixture
def world(tmp_path: Path) -> World:
    defaults = tomllib.loads((ROOT / "settings.toml").read_text())
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "queue").mkdir(mode=0o755)  # the warden's own folder in the checkout, where it files requests
    (checkout / "queue").chmod(0o755)
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
        launcher=FakeStack(tmp_path),  # type: ignore[arg-type]  # the tick runs only scripts/dev on slot 0
        github=github,
        post_event=lambda kind, data: events.append((kind, data)),
    )
    return World(ctx, github, clock, events, checkout)


@pytest.fixture
def github_repo(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A bare repo standing in for GitHub, reached through the real url, and the agent's checkout cloned from it."""
    remote = tmp_path / "github.git"
    config = tmp_path / "gitconfig"
    config.write_text(
        f'[url "{remote}"]\n\tinsteadOf = git@github.com:owner/name.git\n'
        "[user]\n\tname = Tester\n\temail = tester@example.invalid\n[init]\n\tdefaultBranch = master\n"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    git(tmp_path, "init", "-q", "--bare", str(remote))
    world.checkout.mkdir(exist_ok=True)
    git(world.checkout, "init", "-q")
    # The settings name the hook scripts a push refuses to change.
    shutil.copy(ROOT / "testdata" / "fixture-product" / "factory.toml", world.checkout / "factory.toml")
    commit(world.checkout, write(".github/workflows/ci.yml"))
    git(world.checkout, "push", "-q", str(remote), "master")
    key = github_push.deploy_key(world.ctx.places)
    key.parent.mkdir(parents=True)
    key.write_text("not a real key\n")
    return remote
