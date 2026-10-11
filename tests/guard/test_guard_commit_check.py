"""The product's git hooks: inside the factory's slice no commit or merge into the default branch changes a protected
path, nothing is committed to the factory's own repo and nothing is pushed; a merge whose tree passed is not checked
again."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from runtime.guard.commit_check import HookError, is_factory_repo, passed, refusal, turn_refusal
from runtime.guard.writes import ALWAYS_PROTECTED
from runtime.shared import store
from runtime.shared.paths import INSTALL, Places

HOOKS = Path(__file__).resolve().parents[2] / "checks" / "git-hooks"


@pytest.mark.parametrize(
    ("paths", "in_slice", "hook", "on_default", "merging", "refused"),
    [
        (["GOAL.md"], True, "pre-commit", False, False, True),
        (["goal/hero.png"], True, "pre-commit", True, False, True),
        (["factory.toml"], True, "pre-commit", False, False, True),
        ([".github/workflows/deploy-prod.yml"], True, "pre-commit", False, False, True),
        (["check-values/surfaces.toml"], True, "pre-commit", False, False, True),
        (["src/app.py", "docs/GOAL.md", "goals.md"], True, "pre-commit", True, False, False),
        (["GOAL.md"], False, "pre-commit", True, False, False),
        (["GOAL.md"], True, "pre-merge-commit", True, True, True),
        (["GOAL.md"], True, "pre-merge-commit", False, True, False),
        ([], True, "pre-push", True, False, True),
        ([], False, "pre-push", True, False, False),
    ],
)
def test_inside_the_slice_no_change_to_a_protected_path_lands_and_nothing_is_pushed(
    paths: list[str], in_slice: bool, hook: str, on_default: bool, merging: bool, refused: bool
) -> None:
    reason = refusal(
        paths,
        prefixes=ALWAYS_PROTECTED,
        in_slice=in_slice,
        hook=hook,
        on_default=on_default,
        merging=merging,
        factory=False,
    )
    assert bool(reason) is refused


@pytest.mark.parametrize(
    ("url", "factory"),
    [
        (f"git@github.com:depot/{INSTALL.name}.git", True),
        (f"https://github.com/depot/{INSTALL.name}", True),
        (f"https://github.com/depot/{INSTALL.name}-fixture.git", False),
        ("https://github.com/depot/product.git", False),
        ("", False),
    ],
)
def test_the_factory_never_commits_to_its_own_repo(url: str, factory: bool) -> None:
    assert is_factory_repo(url) is factory
    reason = refusal(
        ["src/x.py"],
        prefixes=ALWAYS_PROTECTED,
        in_slice=True,
        hook="pre-commit",
        on_default=False,
        merging=False,
        factory=factory,
    )
    assert bool(reason) is factory


def test_a_merge_lands_only_with_a_tree_the_warden_recorded_as_passed(tmp_path: Path) -> None:
    places = Places.under(tmp_path)
    tree = "a" * 40
    assert not passed(places, tree)
    store.write_json(places.public_check_passes, {"trees": [tree]})
    assert passed(places, tree) and not passed(places, "b" * 40)
    places.public_check_passes.write_text("{not json")
    with pytest.raises(HookError, match="cannot be read"):
        passed(places, tree)


@pytest.mark.parametrize("hook", ["pre-commit", "pre-merge-commit", "pre-push"])
def test_every_hook_runs_the_installed_check(hook: str) -> None:
    script = HOOKS / hook
    assert script.resolve() == HOOKS / "pre-commit" and script.stat().st_mode & 0o111
    assert subprocess.run(["bash", "-n", str(script)], check=False).returncode == 0


def test_only_the_branch_holding_the_merge_turn_merges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    subprocess.run(["git", "init", "-q", "-b", "master", str(tmp_path / "repo")], check=True)
    monkeypatch.chdir(tmp_path / "repo")
    for branch in ("impl-a", "impl-b"):
        who = ["-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run(["git", *who, "commit", "-q", "--allow-empty", "-m", branch], check=True)
        subprocess.run(["git", "branch", branch], check=True)
    heads = {b: subprocess.run(["git", "rev-parse", b], capture_output=True, text=True, check=True).stdout.strip()
             for b in ("impl-a", "impl-b")}  # fmt: skip
    places = Places.under(tmp_path)

    assert "no task holds the merge turn" in turn_refusal(places, heads["impl-a"])
    store.write_json(places.public_turn, {"task": "t-0001", "branch": "impl-a"})
    assert turn_refusal(places, heads["impl-a"]) == ""
    assert "only impl-a" in turn_refusal(places, heads["impl-b"])
