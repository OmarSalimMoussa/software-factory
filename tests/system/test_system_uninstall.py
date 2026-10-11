from __future__ import annotations

from pathlib import Path

import pytest

from runtime.shared.paths import MANAGED_SETTINGS
from runtime.system import layout, sealed, uninstall
from runtime.system.host import DryHost


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
    copies = [own, sealed.release_folder(own), stale, sealed.release_folder(stale)]
    find = f"find {layout.STAGE_ROOT} -mindepth 1 -maxdepth 1"
    host = DryHost(paths={layout.STAGE_ROOT, *copies}, answers={find: (0, "\n".join(map(str, copies)))})

    uninstall.sealed_copies(host)

    assert (f"remove: {layout.STAGE_ROOT}" in host.actions) is removed
    if not removed:
        # A copy an earlier failed install kept goes; the one this runs from goes when it ends.
        gone = {action.removeprefix("remove: ") for action in host.actions if action.startswith("remove: ")}
        assert gone == {str(stale), str(sealed.release_folder(stale))}
