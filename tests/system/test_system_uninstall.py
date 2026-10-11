from __future__ import annotations

from pathlib import Path

import pytest

from runtime.shared.paths import MANAGED_SETTINGS
from runtime.system import layout, uninstall
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
    host = DryHost(paths={layout.STAGE_ROOT})

    uninstall.sealed_copies(host)

    assert (f"remove: {layout.STAGE_ROOT}" in host.actions) is removed
