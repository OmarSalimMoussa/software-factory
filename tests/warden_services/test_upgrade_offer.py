"""The owner's Upgrade from the dashboard: a newer factory found on GitHub, and the upgrade to it started."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest
from warden_fakes import OWNER_CALLER, FakeLauncher

from runtime.shared import network, releases, upgrades
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused
from runtime.system import layout
from runtime.system.runners import upgrade as upgrade_runner
from runtime.warden.context import Context
from runtime.warden.dispatch import Doors
from runtime.warden.owner import factory_upgrade, ops
from runtime.warden.sessions import lifecycle

OLD, NEW = "a" * 40, "b" * 40
REPO = "acme/software-factory"
ROOT = Path(__file__).resolve().parents[2]


def _installed(places: Places) -> None:
    places.upgrade.mkdir(parents=True)
    upgrades.installed_path(places).write_text(json.dumps({"commit": OLD, "owner": "omar", "product": "/home/o/shop",
                                                           "repo": REPO}))  # fmt: skip
    releases.token_path(places).parent.mkdir(parents=True, exist_ok=True)
    releases.token_path(places).write_text("github_pat_read_only\n")


def _github(calls: list[str], down: bool = False) -> releases.Get:
    """GitHub with one release, NEW, two commits ahead of OLD."""

    def get(url: str, headers: dict[str, str], timeout: float) -> network.Answer:
        calls.append(url)
        if down:
            raise TimeoutError("timed out")
        assert headers["Authorization"] == "Bearer github_pat_read_only"
        if url.endswith("/releases/latest"):
            body = {"target_commitish": NEW, "tag_name": releases.tag(NEW), "published_at": "2026-10-11T18:31:17Z",
                    "author": {"login": releases.CI_BOT}, "assets": []}  # fmt: skip
        elif url.endswith(f"/compare/{OLD}...{NEW}"):
            body = {
                "status": "ahead",
                "total_commits": 2,
                "commits": [
                    {"commit": {"message": "The installer upgrades\n\nmore"}},
                    {"commit": {"message": "Faster checks"}},
                ],
            }
        else:
            raise AssertionError(url)
        return network.Answer(200, {}, json.dumps(body).encode())

    return get


@pytest.fixture
def ctx(doors: Doors, tmp_path: Path) -> Context:
    product = tmp_path / "product"  # the pause ranks the agents by the product's goal
    product.mkdir()
    shutil.copy(ROOT / "testdata" / "fixture-product" / "GOAL.md", product / "GOAL.md")
    doors.places.overrides.parent.mkdir(parents=True, exist_ok=True)
    doors.places.overrides.write_text(f'[repo]\npath = "{product}"\n')
    return doors.context(OWNER_CALLER)


def test_a_newer_release_is_offered_with_its_changelog_once_each_check_period(
    ctx: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    _installed(ctx.places)
    calls: list[str] = []
    monkeypatch.setattr(releases, "http_get", _github(calls))

    factory_upgrade.poll(ctx)
    asked = len(calls)
    factory_upgrade.poll(ctx)  # within install.release_check_hours: GitHub is not asked again

    found = factory_upgrade.offer(ctx.places)
    assert found["note"] is None and len(calls) == asked == 2
    assert found["available"] == {"commit": NEW, "tag": releases.tag(NEW), "published": "2026-10-11T18:31:17Z",
                                  "changelog": ["The installer upgrades", "Faster checks"]}  # fmt: skip


@pytest.mark.parametrize("installed", [False, True], ids=["never-upgraded-by-hand", "github-does-not-answer"])
def test_without_roots_record_or_github_nothing_is_offered_and_the_page_is_told_why(
    ctx: Context, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, installed: bool
) -> None:
    if installed:
        _installed(ctx.places)
    monkeypatch.setattr(releases, "http_get", _github([], down=True))

    with caplog.at_level(logging.WARNING):
        factory_upgrade.poll(ctx)

    found = factory_upgrade.offer(ctx.places)
    assert found["available"] is None
    assert ("GitHub could not be asked" in found["note"]) is installed
    assert [r.getMessage() for r in caplog.records] == (["warden: the release check failed (ReleaseError)"]
                                                         if installed else [])  # fmt: skip


def test_the_owners_upgrade_pauses_waits_for_every_agent_and_starts_roots_unit(
    ctx: Context, launcher: FakeLauncher, monkeypatch: pytest.MonkeyPatch
) -> None:
    _installed(ctx.places)
    monkeypatch.setattr(releases, "http_get", _github([]))
    factory_upgrade.poll(ctx)

    with pytest.raises(Refused, match="not the one on offer"):
        factory_upgrade.start(ctx, {"commit": OLD}, None)
    op_id = factory_upgrade.start(ctx, {"commit": NEW}, None)["op"]
    ops.join(op_id, 10)

    op = ops.get(ctx, {"id": op_id}, None)["op"]
    assert op["state"] == ops.DONE, op["steps"]
    assert [s["text"] for s in op["steps"]] == [
        "Pausing the factory",
        "Waiting for every agent to settle",
        "Starting the upgrade",
    ]
    assert launcher.upgrades == [NEW] and lifecycle.paused_path(ctx).exists()
    asked = factory_upgrade.asked(ctx.places)
    assert asked is not None and (asked["from"], asked["to"], asked["changelog"]) == (
        OLD,
        NEW,
        ["The installer upgrades", "Faster checks"],
    )
    view = factory_upgrade.view(ctx)
    assert (view["installed"], view["op"]["state"], view["paused"]) == (OLD, ops.DONE, True)


def test_an_agent_that_never_settles_stops_the_upgrade_before_it_starts(
    ctx: Context, launcher: FakeLauncher, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _installed(ctx.places)
    monkeypatch.setattr(releases, "http_get", _github([]))
    factory_upgrade.poll(ctx)
    busy = [{"name": "impl-t-0001", "state": "working"}]
    monkeypatch.setattr(
        factory_upgrade.registry, "read", lambda places: {"s1": type("S", (), busy[0])()}
    )  # a session the pause could not bench
    monkeypatch.setattr(factory_upgrade, "_pause", lambda ctx: "paused")
    monkeypatch.setattr(factory_upgrade.time, "sleep", lambda s: setattr(ctx.clock, "t", ctx.clock.now() + 3600))

    op_id = factory_upgrade.start(ctx, {"commit": NEW}, None)["op"]
    ops.join(op_id, 10)

    op = ops.get(ctx, {"id": op_id}, None)["op"]
    assert op["state"] == ops.FAILED and "impl-t-0001 did not settle" in op["steps"][1]["detail"]
    assert launcher.upgrades == []


@pytest.mark.parametrize("started", [True, False], ids=["went-quiet-mid-way", "never-said-a-word"])
def test_an_upgrade_quiet_for_longer_than_one_may_run_reads_as_stopped(
    ctx: Context, monkeypatch: pytest.MonkeyPatch, started: bool
) -> None:
    _installed(ctx.places)
    monkeypatch.setattr(releases, "http_get", _github([]))
    factory_upgrade.poll(ctx)
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
    upgrades.write_progress(places, OLD, NEW, upgrades.RUNNING, "Downloading the release")
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


def test_a_new_install_asks_again_at_once_and_is_never_offered_itself(
    ctx: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    _installed(ctx.places)
    calls: list[str] = []
    monkeypatch.setattr(releases, "http_get", _github(calls))
    factory_upgrade.poll(ctx)
    assert factory_upgrade.view(ctx)["offer"]["available"]["commit"] == NEW

    record = json.loads(upgrades.installed_path(ctx.places).read_text())
    upgrades.installed_path(ctx.places).write_text(json.dumps({**record, "commit": NEW}))
    assert factory_upgrade.view(ctx)["offer"]["available"] is None
    asked = len(calls)
    factory_upgrade.poll(ctx)

    assert len(calls) == asked + 1 and factory_upgrade.offer(ctx.places)["available"] is None


@pytest.mark.parametrize(
    ("now", "shown"),
    [(OLD, True), (NEW, True), ("c" * 40, False)],
    ids=["still-on-the-factory-it-left", "on-the-factory-it-asked-for", "on-one-installed-by-hand-since"],
)
def test_an_upgrade_that_stopped_is_shown_until_an_install_by_hand_replaces_it(
    ctx: Context, monkeypatch: pytest.MonkeyPatch, now: str, shown: bool
) -> None:
    _installed(ctx.places)
    monkeypatch.setattr(releases, "http_get", _github([]))
    factory_upgrade.poll(ctx)
    ops.join(factory_upgrade.start(ctx, {"commit": NEW}, None)["op"], 10)
    upgrades.write_progress(ctx.places, OLD, NEW, upgrades.FAILED, "", "settings: repo.path must be set")

    record = json.loads(upgrades.installed_path(ctx.places).read_text())
    upgrades.installed_path(ctx.places).write_text(json.dumps({**record, "commit": now}))
    view = factory_upgrade.view(ctx)

    assert (view["asked"] is not None, view["progress"] is not None) == (shown, shown)


def test_an_upgrade_that_stopped_asks_github_again_at_once_for_a_release_that_replaced_it(
    ctx: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    _installed(ctx.places)
    calls: list[str] = []
    monkeypatch.setattr(releases, "http_get", _github(calls))
    upgrades.write_progress(ctx.places, OLD, NEW, upgrades.FAILED, "", "The downloaded release file is not the release")
    failed_at = datetime.fromisoformat(upgrades.read_progress(ctx.places)["at"]).timestamp()
    ctx.clock.t = failed_at - 60
    factory_upgrade.poll(ctx)
    asked = len(calls)

    ctx.clock.t = failed_at + 1
    factory_upgrade.poll(ctx)
    again = len(calls)
    factory_upgrade.poll(ctx)  # once for each failure, then once each check period again

    assert (asked, again, len(calls)) == (2, 4, 4)


@pytest.mark.parametrize(
    ("installed", "down", "state", "newer"),
    [(OLD, False, ops.DONE, True), (NEW, False, ops.DONE, False), (OLD, True, ops.FAILED, None)],
    ids=["a-newer-one", "the-newest-already", "github-does-not-answer"],
)
def test_check_now_asks_github_at_once_and_says_whether_a_newer_factory_is_ready(
    ctx: Context, monkeypatch: pytest.MonkeyPatch, installed: str, down: bool, state: str, newer: bool | None
) -> None:
    _installed(ctx.places)
    record = json.loads(upgrades.installed_path(ctx.places).read_text())
    upgrades.installed_path(ctx.places).write_text(json.dumps({**record, "commit": installed}))
    calls: list[str] = []
    monkeypatch.setattr(releases, "http_get", _github(calls))
    factory_upgrade.poll(ctx)
    asked = len(calls)
    monkeypatch.setattr(releases, "http_get", _github(calls, down=down))

    op_id = factory_upgrade.check(ctx, {}, None)["op"]  # within install.release_check_hours of the last check
    ops.join(op_id, 10)

    op = ops.get(ctx, {"id": op_id}, None)["op"]
    assert len(calls) > asked and op["state"] == state
    assert op["result"].get("newer") == newer
    if down:
        assert "GitHub could not be asked" in op["steps"][0]["detail"]
