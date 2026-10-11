"""The upgrade the dashboard starts: root's own records, and an install that asks nothing and says how it goes."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest
from system_fakes import dry_job

from runtime.shared import store, upgrades
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused
from runtime.shared.settings import overrides_toml
from runtime.system import steps_prepare
from runtime.system.host import DeployKey, DryHost
from runtime.system.journal import Journal
from runtime.system.unattended import UnattendedHost

OLD, NEW = "a" * 40, "b" * 40


def _host(tmp_path: Path) -> tuple[UnattendedHost, Places]:
    places = Places.under(tmp_path)
    return UnattendedHost(Journal(verbose=False, terminal=False), places, OLD, NEW), places


@pytest.mark.parametrize(
    "asks",
    [
        lambda h: h.ask("Your GitHub token for the warden: ", secret=True, hint="", link=""),
        lambda h: h.confirm("Sign in to GitHub", "", yes="Done", no="Skip"),
        lambda h: h.show_code("secret", "otpauth://x"),
        lambda h: h.show_key(DeployKey("acme/shop", "t", "ssh-ed25519 AAAA", True, "", "", "", "")),
    ],
    ids=["a-question", "a-confirmation", "a-new-code", "a-key-to-add"],
)
def test_an_upgrade_never_waits_for_an_answer_it_stops_and_says_to_upgrade_by_hand(
    tmp_path: Path, asks: Callable[[UnattendedHost], object]
) -> None:
    host, _ = _host(tmp_path)
    with pytest.raises(Refused, match="Run the release file by hand"):
        asks(host)


def test_each_step_and_a_failure_reach_roots_progress_record(tmp_path: Path) -> None:
    host, places = _host(tmp_path)

    host.step(3, "Copying the factory into place")
    running = upgrades.read_progress(places)
    host.fail("A command it ran failed. Run it again.")
    failed = upgrades.read_progress(places)

    assert running is not None and (running["state"], running["step"]) == (
        upgrades.RUNNING,
        "Copying the factory into place",
    )
    assert failed is not None and (failed["state"], failed["problem"]) == (
        upgrades.FAILED,
        "A command it ran failed. Run it again.",
    )
    assert (failed["from"], failed["to"]) == (OLD, NEW)
    assert oct(upgrades.progress_path(places).stat().st_mode & 0o777) == "0o644"  # the warden reads it, never writes it


@pytest.mark.parametrize(
    "record",
    [{"commit": OLD, "owner": "omar"}, {"commit": "abc1234", "owner": "omar", "product": "/home/omar/shop"}, []],
    ids=["no-product", "short-commit", "not-an-object"],
)
def test_an_install_record_root_cannot_use_is_refused_loudly(tmp_path: Path, record: object) -> None:
    places = Places.under(tmp_path)
    places.upgrade.mkdir(parents=True)
    store.write_json(upgrades.installed_path(places), record)
    with pytest.raises(store.StoreError):
        upgrades.read_installed(places)


@pytest.mark.parametrize("planted", ["link", "fifo", "linked-folder"], ids=["a-link", "a-fifo", "a-linked-folder"])
def test_root_reads_the_warden_s_settings_only_as_a_plain_file_never_through_a_link(
    tmp_path: Path, planted: str
) -> None:
    state, secret = tmp_path / "state", tmp_path / "root-only.txt"
    secret.write_text("root's own\n")
    (state / "settings").mkdir(parents=True)
    rel = Path("settings") / "overrides.toml"
    if planted == "link":
        (state / rel).symlink_to(secret)
    elif planted == "fifo":
        os.mkfifo(state / rel)
    else:
        (state / "settings").rmdir()
        (state / "settings").symlink_to(tmp_path)
        (tmp_path / "overrides.toml").write_text("[caps]\n")

    with pytest.raises(store.StoreError):
        store.read_inside(state, rel)


@pytest.mark.parametrize(
    ("saved", "last", "refused"),
    [({}, {}, None), ({"dashboard.port": 9000}, {}, "dashboard.port"), ({"caps.implementers": 3}, {}, None),
     ({}, None, "kept no record")],
    ids=["as-last-installed", "an-install-setting-changed", "a-live-setting-changed", "no-record-yet"],
)  # fmt: skip
def test_an_unattended_upgrade_applies_only_the_settings_root_applied_last(
    tmp_path: Path, saved: dict[str, object], last: dict[str, object] | None, refused: str | None
) -> None:
    job = dry_job(tmp_path)
    job.unattended = True
    places = job.places
    job.settings = None
    places.overrides.parent.mkdir(parents=True)
    places.overrides.write_text(overrides_toml(saved))
    baseline = _settled_overrides(tmp_path, places)
    if last is not None:
        places.upgrade.mkdir(parents=True)
        store.write_json(upgrades.settings_path(places), {**baseline, **last})
    steps_prepare.check_machine(job)
    if refused:
        with pytest.raises(Refused, match=refused):
            steps_prepare.settle_settings(job)
    else:
        steps_prepare.settle_settings(job)


def _settled_overrides(tmp_path: Path, places: Places) -> dict[str, object]:
    """What a hand install with no saved settings would have recorded on this machine."""
    (tmp_path / "plain").mkdir()
    plain = dry_job(tmp_path / "plain")
    steps_prepare.check_machine(plain)
    steps_prepare.settle_settings(plain)
    return dict(plain.overrides)


@pytest.mark.parametrize(
    "path",
    ["/home/software-agent/../omar/shop", "/srv/shop", "/home/software-agentx/shop"],
    ids=["climbs-out", "elsewhere", "a-lookalike-home"],
)
def test_the_product_s_checkout_must_be_a_folder_of_the_agents_home(tmp_path: Path, path: str) -> None:
    job = dry_job(tmp_path)
    assert isinstance(job.host, DryHost)
    job.host.files[job.places.overrides] = f'[repo]\npath = "{path}"\n'
    steps_prepare.check_machine(job)
    with pytest.raises(Refused, match="repo.path must be a folder inside"):
        steps_prepare.settle_settings(job)
