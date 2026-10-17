from __future__ import annotations

from pathlib import Path

import pytest
from system_fakes import dry_job

from runtime.shared import paths
from runtime.shared.paths import MANAGED_SETTINGS, Places
from runtime.system import install, layout, uninstall
from runtime.system.host import Account, DryHost
from standard.server import setup_steps


@pytest.mark.parametrize(
    ("text", "removed"),
    [
        ('{"hooks": {"PreToolUse": [{"hooks": [{"command": "python3 -I ' + uninstall.GUARD + '"}]}]}}', True),
        ('{"permissions": {"deny": ["Bash(rm *)"]}}', False),  # someone else's managed settings stay
    ],
    ids=["the-factorys", "someone-elses"],
)
def test_the_uninstall_removes_only_the_factorys_managed_settings(text: str, removed: bool) -> None:
    host = DryHost(files={MANAGED_SETTINGS: text})

    uninstall.system_files(host)

    assert (f"remove: {MANAGED_SETTINGS}" in host.actions) is removed


@pytest.mark.parametrize(
    ("runs_from", "removed"),
    [(Path("/home/owner/software-factory"), True), (layout.STAGE_ROOT / "0123abcd-0a1b2c3d", False)],
    ids=["the-owners-checkout", "a-sealed-copy"],
)
def test_the_uninstall_removes_every_sealed_copy_but_the_one_it_runs_from(
    monkeypatch: pytest.MonkeyPatch, runs_from: Path, removed: bool
) -> None:
    monkeypatch.setattr(uninstall, "REPO_ROOT", runs_from)
    own, stale = layout.STAGE_ROOT / "0123abcd-0a1b2c3d", layout.STAGE_ROOT / "4567ef01-2b3c4d5e"
    copies = [own, stale]
    find = f"find {layout.STAGE_ROOT} -mindepth 1 -maxdepth 1"
    host = DryHost(paths={layout.STAGE_ROOT, *copies}, answers={find: (0, "\n".join(map(str, copies)))})

    uninstall.sealed_copies(host)

    assert (f"remove: {layout.STAGE_ROOT}" in host.actions) is removed
    if not removed:
        # A copy an earlier failed install kept goes; the one this runs from goes when it ends.
        gone = {action.removeprefix("remove: ") for action in host.actions if action.startswith("remove: ")}
        assert gone == {str(stale)}


def test_the_uninstall_takes_the_factorys_own_glitchtip_away_with_its_account_and_its_data(tmp_path: Path) -> None:
    host = DryHost(dry=False)
    for uid, user in ((1002, layout.STACK_USER), (1003, layout.ERRORS_USER)):
        host.users[user] = Account(user, uid, uid, Path("/home") / user, (user,), "")
    host.paths.add(paths.ERRORS)

    uninstall.state(host, Places.under(tmp_path))
    uninstall.users(host)

    assert f"remove: {paths.ERRORS}" in host.actions
    assert f"run: userdel --remove {layout.ERRORS_USER}" in host.actions
    assert layout.ERRORS_UNIT in uninstall.UNITS


def test_taking_back_a_failed_first_install_leaves_the_server_set_ups_config_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prods = Path(setup_steps.PRODUCT_TOML)
    job = dry_job(tmp_path)
    assert isinstance(job.host, DryHost)
    job.host.files |= {prods: "[product]\n", MANAGED_SETTINGS: "{}"}
    monkeypatch.setattr(job.host, "exists", lambda path: True)  # whatever the machine holds

    install.roll_back(job)

    removed = [Path(action.removeprefix("remove: ")) for action in job.host.actions if action.startswith("remove: ")]
    assert layout.NFT_FILE in removed
    assert not [path for path in removed if prods.is_relative_to(path)]
