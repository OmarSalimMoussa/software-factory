from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest
from warden_fakes import ROOT, FakeLauncher, defaults_text, git

from runtime.shared import store
from runtime.shared.models import DEPRIORITIZED, Session
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused
from runtime.warden import ops, registry, settings_save
from runtime.warden.context import Context

HAIKU = "claude-haiku-5-5"


@pytest.fixture
def clone(places: Places, tmp_path: Path) -> Path:
    """The factory repo on a bare origin, the warden's clone of it, and an install pointing at the clone."""
    origin, clone = tmp_path / "origin.git", tmp_path / "clone"
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    (seed / "settings.toml").write_text((ROOT / "settings.toml").read_text())
    for args in (("add", "."), ("-c", "user.name=t", "-c", "user.email=t@e.invalid", "commit", "-qm", "seed")):
        git(seed, *args)
    git(tmp_path, "clone", "-q", "--bare", str(seed), str(origin))
    git(tmp_path, "clone", "-q", str(origin), str(clone))
    for key, value in (("user.name", "warden"), ("user.email", "warden@e.invalid"), ("core.hooksPath", "/dev/null")):
        git(clone, "config", key, value)
    places.defaults.write_text(defaults_text(**{"factory.clone": str(clone)}))
    agents = places.install / "plugin" / "agents"
    agents.mkdir(parents=True)
    (agents / "critic-ux.md").write_text(
        '---\nname: critic-ux\nmodel: "{{model}}"\neffort: "{{effort}}"\n---\nBrief.\n'
    )
    for folder in ("procedures", "rules"):
        (places.install / folder).mkdir()
        (places.install / folder / "a.md").write_text("text\n")
    return clone


def save(ctx: Context, **args: Any) -> dict[str, Any]:
    return settings_save.save(ctx, args, None)


@pytest.mark.parametrize(
    ("changes", "base", "rule"),
    [
        ({"caps.implementers": 99}, None, "settings"),
        ({"caps.implementers": 4, "caps.nonsense": 1}, None, "settings"),
        ({"models.critic": {"model": "gpt", "effort": "low"}}, None, "settings"),
        ({"prices": {}}, None, "settings"),
        ({"repo.path": "/elsewhere"}, None, "settings"),
        ({"caps.implementers": 4}, "0000000000000000", "stale"),
    ],
)
def test_a_save_with_any_bad_value_is_refused_and_writes_nothing(
    ctx: Context, clone: Path, changes: dict[str, Any], base: str | None, rule: str
) -> None:
    head = git(clone, "rev-parse", "HEAD")

    with pytest.raises(Refused) as refused:
        save(ctx, changes=changes, **({"base": base} if base else {}))

    assert refused.value.rule == rule
    assert not ctx.places.overrides.exists()
    assert not (ctx.places.state / "ops").exists()
    assert git(clone, "rev-parse", "HEAD") == head


def test_a_save_applies_at_once_and_commits_settings_toml(ctx: Context, clone: Path) -> None:
    critic = {"model": HAIKU, "effort": "low"}

    op_id = save(ctx, changes={"watch.outage_after": 3, "models.critic": critic},
                 base=settings_save.overrides_version({}))["op"]  # fmt: skip
    ops.join(op_id, timeout=30)

    op = ops.get(ctx, {"id": op_id}, None)["op"]
    assert [s["state"] for s in op["steps"]] == ["done", "done", "done"], op["steps"]
    assert stat.S_IMODE(ctx.places.overrides.stat().st_mode) == 0o600
    assert ctx.settings().get_int("watch.outage_after") == 3
    assert json.loads((ctx.places.state / "public" / "settings.json").read_text())["models"]["critic"] == critic
    rendered = (ctx.places.rendered_plugin / "agents" / "critic-ux.md").read_text()
    assert f'model: "{HAIKU}"\neffort: "low"' in rendered
    assert (ctx.places.rendered_plugin.parent / "procedures" / "a.md").exists()

    git(clone, "fetch", "-q")
    pushed = git(clone, "show", "origin/main:settings.toml")
    assert "\noutage_after = 3        # failed prod probes in a row that count as an outage\n" in pushed
    assert '\ncritic = { model = "claude-haiku-5-5", effort = "low" }\n' in pushed
    assert 'path = ""' in pushed  # the product's own values never enter the factory repo
    assert git(clone, "log", "-1", "--format=%s", "origin/main") == "Settings: models.critic, watch.outage_after"
    [audit] = store.read_jsonl(ctx.places.audit)
    assert audit["commit"] == op["result"]["commit"] == git(clone, "rev-parse", "origin/main")
    assert {c["key"]: (c["was"], c["now"]) for c in audit["changes"]}["watch.outage_after"] == (2, 3)


@pytest.mark.parametrize("why", ["own remote", "live session"])
def test_the_repo_never_changes_to_the_factorys_own_or_under_live_sessions(
    ctx: Context, clone: Path, launcher: FakeLauncher, why: str
) -> None:
    git(clone, "remote", "set-url", "origin", "git@github.com:depot/factory.git")
    if why == "own remote":
        launcher.commands[("git", "remote", "get-url")] = (0, "https://github.com/Depot/Factory\n")
    else:
        live = Session(id="s1", name="impl-s1", role="implementer", family="impl", model="m", effort="high",
                       task="t-1", parent="", worktree="/w", slot=1, state=DEPRIORITIZED,
                       started="2026-10-09T10:00:00Z", started_by="orchestrator")  # fmt: skip
        registry.write(ctx.places, {"s1": live})
        launcher.commands[("git", "remote", "get-url")] = (0, "git@github.com:depot/other.git\n")

    with pytest.raises(Refused) as refused:
        settings_save.change_repo(ctx, {"changes": {"repo.path": "/srv/other", "repo.github": "depot/other"}}, None)

    assert refused.value.rule == "repo"
    assert not ctx.places.overrides.exists()
