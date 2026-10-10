"""Branches in a shared `.git`, and the way to prod: a session moves only its own branch, and nothing it runs ships
anything but the checked commit through the warden."""

from __future__ import annotations

from pathlib import Path

import pytest
from guard_kit import Kit


@pytest.fixture
def kit(tmp_path: Path) -> Kit:
    made = Kit(tmp_path)
    made.git_product()
    made.session("implementer")
    return made


@pytest.mark.parametrize(
    ("command", "rule"),
    [
        ("git update-ref refs/heads/other HEAD", "own-branch"),
        ("git update-ref refs/heads/impl-a HEAD", "git-bypass"),
        ("git branch -f other HEAD", "own-branch"),
        ("git branch -D other", "own-branch"),
        ("git branch -m other renamed", "own-branch"),
        ("git branch newthing", "own-branch"),
        ("git checkout -b feature", "own-branch"),
        ("git switch other", "own-branch"),
        ("git checkout other", "own-branch"),
        ("git checkout --detach", "own-branch"),
        ("git checkout -", "own-branch"),
        ("git fetch . impl-a:main", "own-branch"),
        ("git rebase main other", "own-branch"),
        ("git tag v1", "own-branch"),
        ("git worktree add ../x", "own-branch"),
        ("git worktree remove ../x", "own-branch"),
        ("git --git-dir=../.git commit -m x", "own-branch"),
        ("git --work-tree=../x checkout other", "own-branch"),
        ("git stash branch elsewhere", "own-branch"),
        ("git commit -m x", None),
        ("git commit --amend -m x", None),
        ("git merge main", None),
        ("git rebase main", None),
        ("git reset --hard main", None),
        ("git stash", None),
        ("git checkout -- README.md", None),
        ("git checkout README.md", None),
        ("git checkout main -- README.md", None),
        ("git checkout impl-a", None),
        ("git branch", None),
        ("git branch --show-current", None),
        ("git tag -l", None),
        ("git worktree list", None),
    ],
)
def test_a_session_moves_only_its_own_branch(kit: Kit, command: str, rule: str | None) -> None:
    assert kit.bash(command) == rule


@pytest.mark.parametrize("command", ["git -C {repo} commit -m x", "cd {repo} && git commit -m x"])
def test_a_commit_in_another_checkout_would_move_its_branch_and_is_refused(kit: Kit, command: str) -> None:
    assert kit.bash(command.format(repo=kit.repo)) == "own-branch"


def test_the_orchestrator_leaves_finished_branches_to_the_warden(kit: Kit) -> None:
    kit.session("orchestrator", worktree=kit.repo)
    assert kit.bash("git branch -d impl-a", cwd=str(kit.repo)) == "own-branch"
    assert kit.bash("git merge --no-ff impl-a -m 'Merge impl-a'", cwd=str(kit.repo)) is None


@pytest.mark.parametrize(
    ("command", "rule"),
    [
        ("scripts/ci/promote abc1234", "prod-frozen"),
        ("bash scripts/ci/deploy-staging abc1234", "prod-frozen"),
        ("cd scripts/ci && ./promote abc1234", "prod-frozen"),
        ("gh workflow run deploy-prod.yml", "prod-frozen"),
        ("gh run rerun 123", "prod-frozen"),
        ("gh api -X POST repos/o/r/actions/workflows/9/dispatches", "prod-frozen"),
        ("git push origin v1.0", "prod-frozen"),
        ("git push --tags origin", "prod-frozen"),
        ("git push origin HEAD:release", "prod-frozen"),
        ("git push origin abc1234:main", "git-bypass"),
        ("scripts/ci/build abc1234", None),
        ("gh run list --limit 3", None),
    ],
)
def test_nothing_the_orchestrator_runs_reaches_prod(tmp_path: Path, command: str, rule: str | None) -> None:
    kit = Kit(tmp_path)
    kit.session("orchestrator", worktree=kit.repo)
    (kit.repo / "scripts" / "ci").mkdir(parents=True)
    assert kit.bash(command, cwd=str(kit.repo)) == rule
