from __future__ import annotations

import io
import subprocess
import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest

from checks.runner import cli, passes
from checks.standard import tests as tests_check
from runtime.shared.settings import Settings, build

FACTORY = Path(__file__).resolve().parents[2]


def settings() -> Settings:
    defaults = tomllib.loads((FACTORY / "settings.toml").read_text())
    return build(
        defaults,
        {
            "repo.path": "/product",
            "repo.default_branch": "main",
            "repo.github": "owner/product",
            "repo.staging_health": "https://staging.invalid/health",
            "repo.prod_health": "https://prod.invalid/health",
            "repo.promote_workflow": "deploy-prod.yml",
            "repo.owner_login": "owner",
        },
    )


def check(root: Path, *argv: str) -> tuple[int, str]:
    out = io.StringIO()
    return cli.run(list(argv), root, settings(), out), out.getvalue()


def tree(root: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_the_whole_set_passes_on_the_fixture_and_records_its_tree(
    fixture_product: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CI", "true")
    status, output = check(fixture_product, "--all")
    assert status == 0, output
    assert passes.passed(fixture_product, tree(fixture_product))


def test_a_pass_covers_only_its_own_tree(
    fixture_product: Path, monkeypatch: pytest.MonkeyPatch, commit: Callable[[Path, str], None]
) -> None:
    monkeypatch.setenv("CI", "true")
    subprocess.run(["git", "checkout", "-q", "-b", "work"], cwd=fixture_product, check=True)
    notes = fixture_product / "app" / "notes.py"
    notes.write_text(notes.read_text() + "\n\nLIMIT = 500\n")
    commit(fixture_product, "a limit")
    status, output = check(fixture_product)
    assert status == 0, output
    passed = tree(fixture_product)
    notes.write_text(notes.read_text() + "\nWIDTH = 80\n")
    commit(fixture_product, "a width")
    assert passes.passed(fixture_product, passed)
    assert not passes.passed(fixture_product, tree(fixture_product))


@pytest.mark.parametrize(
    ("argv", "dirty", "said"),
    [
        (["--only", "qa-scenarios"], False, "no pass is recorded"),
        ([], True, "commit your changes first"),
    ],
)
def test_a_partial_or_uncommitted_run_records_no_pass(
    fixture_product: Path,
    monkeypatch: pytest.MonkeyPatch,
    commit: Callable[[Path, str], None],
    argv: list[str],
    dirty: bool,
    said: str,
) -> None:
    monkeypatch.setenv("CI", "true")
    subprocess.run(["git", "checkout", "-q", "-b", "work"], cwd=fixture_product, check=True)
    (fixture_product / "qa" / "scenarios" / "README.md").write_text("# QA scenarios\n\nEach opens with its labels.\n")
    if not dirty:
        commit(fixture_product, "words")
    status, output = check(fixture_product, *argv)
    assert said in output
    assert status == (2 if dirty else 0)
    assert not passes.passed(fixture_product, tree(fixture_product))


@pytest.mark.parametrize(
    ("changed", "picked", "unplaced"),
    [
        (("app/notes.py", "README.md"), ["tests"], []),
        (("app/sub/deep.py", "web/app.js"), ["sub-tests", "tests"], []),
        (("README.md", "qa/scenarios/x.md"), [], []),
        (("pyproject.toml",), None, []),
        (("Makefile",), None, ["Makefile"]),
    ],
)
def test_changed_paths_pick_tests_by_their_longest_prefix(
    changed: tuple[str, ...], picked: list[str] | None, unplaced: list[str]
) -> None:
    values = {
        "always": [],
        "full": ["pyproject.toml"],
        "skip_suffixes": [".md"],
        "readers": {"app/": ["tests"], "app/sub/": ["sub-tests", "tests"], "web/": []},
    }
    assert tests_check.select(changed, values) == (picked, unplaced)
