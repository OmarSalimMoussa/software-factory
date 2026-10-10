"""A product's own protected paths (repo.protected_paths) are refused at every door: the guard's write rule, the
git hook's commit check and the warden's push."""

from __future__ import annotations

from pathlib import Path

import pytest
from guard_kit import Kit

from runtime.guard.commit_check import refusal
from runtime.guard.facts import read_settings
from runtime.guard.writes import protected_paths
from runtime.warden import github_push


@pytest.mark.parametrize(
    ("path", "refused"),
    [
        ("scripts/deploy.sh", True),
        ("deploy/prod/run.sh", True),
        ("scripts/ci/build", True),
        ("GOAL.md", True),
        ("scripts/deploy.sh.bak", False),
        ("src/deploy/app.py", False),
    ],
)
@pytest.mark.parametrize("door", ["guard", "hook", "push"])
def test_a_protected_path_is_refused_at_every_door(tmp_path: Path, door: str, path: str, refused: bool) -> None:
    kit = Kit(tmp_path)
    kit.session("implementer")
    settings = read_settings(kit.places)
    if door == "guard":
        found = kit.rule("Write", {"file_path": str(kit.worktree / path), "content": "x"}) == "protected-paths"
    elif door == "hook":
        prefixes = protected_paths(settings)
        found = bool(refusal([path], prefixes=prefixes, in_slice=True, hook="pre-commit", on_default=False,
                             merging=False, factory=False))  # fmt: skip
    else:
        found = github_push.protected(path, settings)
    assert found is refused
