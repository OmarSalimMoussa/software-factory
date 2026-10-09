from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

FIXTURE = Path(__file__).resolve().parents[2] / "testdata" / "fixture-product"
MakeRepo = Callable[[dict[str, str]], Path]
Commit = Callable[[Path, str], None]


def commit_all(root: Path, message: str = "fixture") -> None:
    for args in (["add", "-A"], ["commit", "-q", "--no-verify", "-m", message]):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def git_init(root: Path) -> None:
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "tester@example.invalid"],
        ["config", "user.name", "Tester"],
        ["config", "core.hooksPath", "/dev/null"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def commit() -> Commit:
    """Stage everything in a repo and commit it."""
    return commit_all


@pytest.fixture
def make_repo(tmp_path: Path) -> MakeRepo:
    """A committed git repo holding ``files`` (path -> text); a path ending in ``*`` is made executable."""

    def make(files: dict[str, str]) -> Path:
        root = tmp_path / "repo"
        root.mkdir()
        git_init(root)
        for rel, text in files.items():
            path = root / rel.rstrip("*")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            if rel.endswith("*"):
                path.chmod(0o755)
        commit_all(root)
        return root

    return make


@pytest.fixture
def fixture_product(tmp_path: Path) -> Path:
    """A committed copy of testdata/fixture-product, on branch main."""
    root = tmp_path / "product"
    shutil.copytree(FIXTURE, root, ignore=shutil.ignore_patterns("__pycache__", ".*_cache", "queue"))
    git_init(root)
    commit_all(root)
    return root
