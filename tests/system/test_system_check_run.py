"""A check runs walled into its checkout: product code it runs writes only there and in the tools' caches, reads no
other worktree and no Claude folder, and cannot change the git folder the merge hook and every worktree share."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from runtime.system.runners import check_run
from runtime.system.runners import launch_file as lf

BWRAP = shutil.which("bwrap")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                   capture_output=True)  # fmt: skip


def _home(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """An agent's home: Claude's folder, a cache, the main checkout tracking its project's Claude settings, as a product
    may, and two sessions' worktrees."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text("token")
    (home / ".cache" / "uv").mkdir(parents=True)
    main = home / "shop"
    main.mkdir()
    _git(main, "init", "-q", "-b", "master")
    (main / "app.py").write_text("print('hi')\n")
    (main / ".claude").mkdir()
    (main / ".claude" / "settings.json").write_text("{}\n")
    _git(main, "add", "app.py", ".claude/settings.json")
    _git(main, "commit", "-q", "-m", "first")
    for name in ("impl-a", "impl-b"):
        _git(main, "worktree", "add", "-q", "-b", name, str(main / ".claude" / "worktrees" / name))
    return home, main, main / ".claude" / "worktrees" / "impl-a", main / ".claude" / "worktrees" / "impl-b"


def _try(home: Path, checkout: Path, script: str, meanwhile: Callable[[], object] = lambda: None) -> int:
    """``script`` run inside the walls, after ``meanwhile`` ran outside them once they were drawn."""
    main, common = check_run.git_folders(checkout, 30)
    walls = check_run.walls(checkout, main, common, home, [home / ".cache" / "uv"])
    meanwhile()
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
        ("echo x > .claude/settings.local.json", False),
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
        "own-claude-settings",
    ],  # fmt: skip
)
def test_a_worktree_check_writes_only_its_checkout_and_caches(tmp_path: Path, script: str, allowed: bool) -> None:
    home, main, mine, other = _home(tmp_path)
    names = {"$CACHE": str(home / ".cache" / "uv"), "$HOME_": str(home), "$OTHER": str(other), "$MAIN": str(main)}
    for name, value in names.items():
        script = script.replace(name, value)
    assert (_try(home, mine, script) == 0) is allowed


@pytest.mark.skipif(BWRAP is None, reason="bubblewrap is not installed here")
def test_the_main_checkout_s_check_writes_it_but_not_its_git_folder_its_claude_settings_or_any_worktree(
    tmp_path: Path,
) -> None:
    home, main, mine, _ = _home(tmp_path)
    assert _try(home, main, "echo x > made.txt") == 0
    assert _try(home, main, "echo x >> .git/config") != 0
    assert _try(home, main, "echo x >> .claude/settings.json") != 0
    assert _try(home, main, f"ls {mine}") != 0


@pytest.mark.skipif(BWRAP is None, reason="bubblewrap is not installed here")
def test_the_main_checkout_s_check_sees_every_file_it_tracks_so_git_finds_it_clean(tmp_path: Path) -> None:
    home, main, _, _ = _home(tmp_path)
    (main / "made.txt").write_text("untracked\n")

    assert _try(home, main, 'test -z "$(git status --porcelain --untracked-files=no)"') == 0


@pytest.mark.skipif(BWRAP is None, reason="bubblewrap is not installed here")
def test_a_worktree_made_while_the_main_checkout_is_checked_stays_hidden(tmp_path: Path) -> None:
    home, main, _, _ = _home(tmp_path)
    worktrees = main / ".claude" / "worktrees"
    shutil.rmtree(worktrees)  # before any session's first worktree
    late = worktrees / "impl-c"

    def made() -> None:
        late.mkdir(parents=True)
        (late / "secret.txt").write_text("another session's work\n")

    assert _try(home, main, f"! cat {late / 'secret.txt'}", made) == 0


def test_a_claude_folder_the_branch_made_a_link_is_refused(tmp_path: Path) -> None:
    home, main, _, _ = _home(tmp_path)
    (main / ".claude").rename(tmp_path / "moved")
    (main / ".claude").symlink_to(home / ".claude")

    with pytest.raises(lf.BadRequest, match="is a link"):
        check_run.walls(main, main, main / ".git", home, [])
