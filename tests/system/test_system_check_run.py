"""A check runs walled into its checkout: product code it runs writes only there and in the tools' caches, reads no
other worktree and no Claude folder, and cannot change the git folder the merge hook and every worktree share."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from runtime.system.runners import check_run

BWRAP = shutil.which("bwrap")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                   capture_output=True)  # fmt: skip


def _home(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """An agent's home: Claude's folder, a cache, the main checkout and two sessions' worktrees."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text("token")
    (home / ".cache" / "uv").mkdir(parents=True)
    main = home / "shop"
    main.mkdir()
    _git(main, "init", "-q", "-b", "master")
    (main / "app.py").write_text("print('hi')\n")
    _git(main, "add", "app.py")
    _git(main, "commit", "-q", "-m", "first")
    for name in ("impl-a", "impl-b"):
        _git(main, "worktree", "add", "-q", "-b", name, str(main / ".claude" / "worktrees" / name))
    return home, main, main / ".claude" / "worktrees" / "impl-a", main / ".claude" / "worktrees" / "impl-b"


def _try(home: Path, checkout: Path, script: str) -> int:
    main, common = check_run.git_folders(checkout, 30)
    walls = check_run.walls(checkout, main, common, home, [home / ".cache" / "uv"])
    argv = [str(BWRAP), "--die-with-parent", "--unshare-pid", *walls, "--chdir", str(checkout), "/bin/sh", "-c", script]
    return subprocess.run(argv, capture_output=True, check=False).returncode


@pytest.mark.skipif(BWRAP is None, reason="bubblewrap is not installed here")
@pytest.mark.parametrize(
    ("script", "allowed"),
    [
        ("echo x > made.txt", True),
        ("echo x > $CACHE/made.txt", True),
        ("cat $HOME_/.claude/.credentials.json", False),
        ("ls $OTHER", False),
        ("echo x > $OTHER/made.txt", False),
        ("echo x > $MAIN/app.py", False),
        ("echo 'exit 0' > $MAIN/.git/hooks/pre-merge-commit", False),
        ("echo x >> $MAIN/.git/config", False),
        ("git status --porcelain", True),
    ],
    ids=[
        "own-checkout",
        "cache",
        "credentials",
        "read-other-worktree",
        "write-other-worktree",
        "main-checkout",
        "plant-hook",
        "git-config",
        "git-reads",
    ],  # fmt: skip
)
def test_a_worktree_check_writes_only_its_checkout_and_caches(tmp_path: Path, script: str, allowed: bool) -> None:
    home, main, mine, other = _home(tmp_path)
    names = {"$CACHE": str(home / ".cache" / "uv"), "$HOME_": str(home), "$OTHER": str(other), "$MAIN": str(main)}
    for name, value in names.items():
        script = script.replace(name, value)
    assert (_try(home, mine, script) == 0) is allowed


@pytest.mark.skipif(BWRAP is None, reason="bubblewrap is not installed here")
def test_the_main_checkout_s_check_writes_it_but_not_its_git_folder_or_any_worktree(tmp_path: Path) -> None:
    home, main, mine, _ = _home(tmp_path)
    assert _try(home, main, "echo x > made.txt") == 0
    assert _try(home, main, "echo x >> .git/config") != 0
    assert _try(home, main, f"ls {mine}") != 0
