"""The owner's upgrade of the factory to any commit on GitHub: the factory paused, its agents settled, root's unit
started."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest
from warden_fakes import OWNER_CALLER, FakeLauncher

from runtime.shared import upgrades
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused
from runtime.system import layout
from runtime.system.runners import upgrade as upgrade_runner
from runtime.warden.context import Context
from runtime.warden.dispatch import Doors
from runtime.warden.owner import factory_upgrade, ops
from runtime.warden.sessions import lifecycle

OLD, NEW, BEFORE = "a" * 40, "b" * 40, "9" * 40
REPO = "acme/software-factory"
ROOT = Path(__file__).resolve().parents[2]


def _installed(places: Places) -> None:
    places.upgrade.mkdir(parents=True)
    upgrades.installed_path(places).write_text(json.dumps({"commit": OLD, "owner": "omar", "product": "/home/o/shop",
                                                           "repo": REPO}))  # fmt: skip


@pytest.fixture
def ctx(doors: Doors, tmp_path: Path) -> Context:
    product = tmp_path / "product"  # the pause ranks the agents by the product's goal
    product.mkdir()
    shutil.copy(ROOT / "testdata" / "fixture-product" / "GOAL.md", product / "GOAL.md")
    doors.places.overrides.parent.mkdir(parents=True, exist_ok=True)
    doors.places.overrides.write_text(f'[repo]\npath = "{product}"\n')
    return doors.context(OWNER_CALLER)


@pytest.mark.parametrize("commit", [NEW, BEFORE], ids=["a-commit-after", "the-commit-before"])
def test_the_owners_upgrade_to_any_commit_pauses_waits_for_every_agent_and_starts_roots_unit(
    ctx: Context, launcher: FakeLauncher, commit: str
) -> None:
    _installed(ctx.places)

    op_id = factory_upgrade.start(ctx, {"commit": commit}, None)["op"]
    ops.join(op_id, 10)

    op = ops.get(ctx, {"id": op_id}, None)["op"]
    assert op["state"] == ops.DONE, op["steps"]
    assert [s["text"] for s in op["steps"]] == [
        "Pausing the factory",
        "Waiting for every agent to settle",
        "Starting the upgrade",
    ]
    assert launcher.upgrades == [commit] and lifecycle.paused_path(ctx).exists()
    asked = factory_upgrade.asked(ctx.places)
    assert asked is not None and (asked["from"], asked["to"]) == (OLD, commit)
    view = factory_upgrade.view(ctx)
    assert (view["installed"], view["op"]["state"], view["paused"]) == (OLD, ops.DONE, True)


@pytest.mark.parametrize(
    ("commit", "installed", "says"),
    [("main", True, "full 40-character sha"), (NEW[:12], True, "full 40-character sha"),
     (NEW.upper(), True, "full 40-character sha"), (None, True, "full 40-character sha"),
     (NEW, False, "Install the factory by hand first")],
    ids=["a-branch", "a-short-sha", "upper-case", "none", "nothing-installed"],
)  # fmt: skip
def test_an_upgrade_names_a_full_commit_of_an_installed_factory(
    ctx: Context, launcher: FakeLauncher, commit: str | None, installed: bool, says: str
) -> None:
    if installed:
        _installed(ctx.places)

    with pytest.raises(Refused, match=says):
        factory_upgrade.start(ctx, {"commit": commit}, None)
    assert launcher.upgrades == [] and factory_upgrade.asked(ctx.places) is None


@pytest.mark.parametrize(
    ("busy", "said"),
    [("agent", "impl-t-0001 did not settle"), ("check", "orchestrator's check did not settle"),
     ("push", "the push to prod did not settle")],
    ids=["a-working-agent", "a-check-running-tests", "a-push-to-prod"],
)  # fmt: skip
def test_an_agent_check_or_push_that_never_settles_stops_the_upgrade_before_it_starts(
    ctx: Context, launcher: FakeLauncher, monkeypatch: pytest.MonkeyPatch, busy: str, said: str
) -> None:
    _installed(ctx.places)
    working = {"s1": type("S", (), {"name": "impl-t-0001", "state": "working"})()} if busy == "agent" else {}
    monkeypatch.setattr(factory_upgrade.registry, "read", lambda places: working)  # a session the pause can't bench
    if busy == "check":
        assert ctx.check_places.take(1, "orchestrator", 0) is None  # stopping the factory would kill its run
    if busy == "push":
        monkeypatch.setattr(factory_upgrade.prod_push, "running", lambda ctx: "op-1")
    monkeypatch.setattr(factory_upgrade, "_pause", lambda ctx: "paused")
    monkeypatch.setattr(factory_upgrade.time, "sleep", lambda s: setattr(ctx.clock, "t", ctx.clock.now() + 3600))

    op_id = factory_upgrade.start(ctx, {"commit": NEW}, None)["op"]
    ops.join(op_id, 10)

    op = ops.get(ctx, {"id": op_id}, None)["op"]
    assert op["state"] == ops.FAILED and said in op["steps"][1]["detail"]
    assert launcher.upgrades == []


@pytest.mark.parametrize("started", [True, False], ids=["went-quiet-mid-way", "never-said-a-word"])
def test_an_upgrade_quiet_for_longer_than_one_may_run_reads_as_stopped(ctx: Context, started: bool) -> None:
    _installed(ctx.places)
    ops.join(factory_upgrade.start(ctx, {"commit": NEW}, None)["op"], 10)
    if started:
        upgrades.write_progress(ctx.places, OLD, NEW, upgrades.RUNNING, "Copying the factory into place")
        ctx.clock.t = datetime.fromisoformat(upgrades.read_progress(ctx.places)["at"]).timestamp()
    limit = ctx.settings().get_int("install.upgrade_minutes") * 60

    ctx.clock.t += limit - 1
    before = factory_upgrade.view(ctx)["progress"]
    ctx.clock.t += 2
    after = factory_upgrade.view(ctx)["progress"]

    assert (before or {}).get("state") in ((upgrades.RUNNING,) if started else (None,))
    assert after is not None and after["state"] == upgrades.FAILED and "went quiet" in after["problem"]
    factory_upgrade.start(ctx, {"commit": NEW}, None)


def test_the_units_last_word_marks_a_killed_upgrade_stopped_and_brings_the_dashboard_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    places = Places.under(tmp_path)
    upgrades.write_progress(places, OLD, NEW, upgrades.RUNNING, upgrade_runner.DOWNLOADING)
    monkeypatch.setenv("SERVICE_RESULT", "timeout")
    ran: list[list[str]] = []

    def run(argv: list[str], check: bool) -> subprocess.CompletedProcess[str]:
        ran.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(upgrade_runner.subprocess, "run", run)

    assert upgrade_runner.ended(places, NEW) == 0
    said = upgrades.read_progress(places)
    assert said is not None and said["state"] == upgrades.FAILED and "(timeout)" in said["problem"]
    assert ran == [["systemctl", "start", "--no-block", *layout.SERVICES]]


@pytest.mark.parametrize(
    ("now", "shown"),
    [(OLD, True), (NEW, True), ("c" * 40, False)],
    ids=["still-on-the-factory-it-left", "on-the-factory-it-asked-for", "on-one-installed-by-hand-since"],
)
def test_an_upgrade_that_stopped_is_shown_until_an_install_by_hand_replaces_it(
    ctx: Context, now: str, shown: bool
) -> None:
    _installed(ctx.places)
    ops.join(factory_upgrade.start(ctx, {"commit": NEW}, None)["op"], 10)
    upgrades.write_progress(ctx.places, OLD, NEW, upgrades.FAILED, "", "settings: repo.path must be set")

    record = json.loads(upgrades.installed_path(ctx.places).read_text())
    upgrades.installed_path(ctx.places).write_text(json.dumps({**record, "commit": now}))
    view = factory_upgrade.view(ctx)

    assert (view["asked"] is not None, view["progress"] is not None) == (shown, shown)
