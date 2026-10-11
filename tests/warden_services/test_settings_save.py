from __future__ import annotations

import json
import stat
from typing import Any

import pytest
from warden_fakes import FakeLauncher, defaults_text

from runtime.shared import store
from runtime.shared.models import DEPRIORITIZED, Session
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused
from runtime.warden import ops, registry, settings_save
from runtime.warden.context import Context

HAIKU = "claude-haiku-5-5"


@pytest.fixture
def install(places: Places) -> None:
    """An install whose defaults name the factory's own repo, with launchers that ask for a model."""
    places.defaults.write_text(defaults_text(**{"factory.github": "depot/factory"}))
    agents = places.install / "plugin" / "agents"
    agents.mkdir(parents=True)
    (agents / "critic-ux.md").write_text(
        '---\nname: critic-ux\nmodel: "{{model}}"\neffort: "{{effort}}"\n---\nBrief.\n'
    )
    for folder in ("procedures", "rules"):
        (places.install / folder).mkdir()
        (places.install / folder / "a.md").write_text("text\n")


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
        ({"dashboard.port": 9000}, None, "settings"),  # wired into the firewall and the tailnet at install
        ({"qa.widths": []}, None, "settings"),  # a tester browser needs a width
        ({"qa.widths": [1440, "wide"]}, None, "settings"),
        ({"sandbox.allowed_domains": ["example.org", " "]}, None, "settings"),
    ],
)
@pytest.mark.usefixtures("install")
def test_a_save_with_any_bad_value_is_refused_and_writes_nothing(
    ctx: Context, changes: dict[str, Any], base: str | None, rule: str
) -> None:
    with pytest.raises(Refused) as refused:
        save(ctx, changes=changes, **({"base": base} if base else {}))

    assert refused.value.rule == rule
    assert not ctx.places.overrides.exists()
    assert not (ctx.places.state / "ops").exists()


@pytest.mark.usefixtures("install")
def test_a_save_applies_at_once_stays_on_this_machine_and_is_audited(ctx: Context) -> None:
    critic = {"model": HAIKU, "effort": "low"}
    defaults = ctx.places.defaults.read_text()

    op_id = save(ctx, changes={"watch.outage_after": 3, "models.critic": critic},
                 base=settings_save.overrides_version({}))["op"]  # fmt: skip
    ops.join(op_id, timeout=30)

    op = ops.get(ctx, {"id": op_id}, None)["op"]
    assert [s["state"] for s in op["steps"]] == ["done", "done"], op["steps"]
    assert stat.S_IMODE(ctx.places.overrides.stat().st_mode) == 0o600
    assert ctx.settings().get_int("watch.outage_after") == 3
    assert json.loads((ctx.places.state / "public" / "settings.json").read_text())["models"]["critic"] == critic
    rendered = (ctx.places.rendered_plugin / "agents" / "critic-ux.md").read_text()
    assert f'model: "{HAIKU}"\neffort: "low"' in rendered
    assert (ctx.places.rendered_plugin.parent / "procedures" / "a.md").exists()
    assert ctx.places.defaults.read_text() == defaults  # the installed release's defaults are never written
    [audit] = store.read_jsonl(ctx.places.audit)
    assert "commit" not in audit
    assert {c["key"]: (c["was"], c["now"]) for c in audit["changes"]}["watch.outage_after"] == (2, 3)


@pytest.mark.parametrize("why", ["own remote", "live session"])
@pytest.mark.usefixtures("install")
def test_the_repo_never_changes_to_the_factorys_own_or_under_live_sessions(
    ctx: Context, launcher: FakeLauncher, why: str
) -> None:
    if why == "own remote":
        launcher.commands[("git", "remote", "get-url")] = (0, "https://github.com/Depot/Factory\n")
    else:
        live = Session(id="s1", name="impl-s1", role="implementer", family="impl", model="m", effort="high",
                       task="t-1", parent="", worktree="/w", state=DEPRIORITIZED,
                       started="2026-10-09T10:00:00Z", started_by="orchestrator")  # fmt: skip
        registry.write(ctx.places, {"s1": live})
        launcher.commands[("git", "remote", "get-url")] = (0, "git@github.com:depot/other.git\n")

    with pytest.raises(Refused) as refused:
        settings_save.change_repo(ctx, {"changes": {"repo.path": "/srv/other", "repo.github": "depot/other"}}, None)

    assert refused.value.rule == "repo"
    assert not ctx.places.overrides.exists()
