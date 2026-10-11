"""The upgrade the dashboard starts: root's own records, and an install that asks nothing and says how it goes."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from runtime.shared import store, upgrades
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused
from runtime.system.host import DeployKey
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
