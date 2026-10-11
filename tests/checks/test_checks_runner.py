from __future__ import annotations

import io
import subprocess
import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest

from checks.runner import cli
from checks.runner.repo import Repo
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
        },
    )


def check(root: Path, *argv: str) -> tuple[int, str]:
    out = io.StringIO()
    return cli.run(list(argv), root, settings(), out), out.getvalue()


def test_the_whole_set_passes_on_the_fixture(fixture_product: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CI", "true")
    status, output = check(fixture_product, "--all")
    assert status == 0, output


def test_since_a_commit_checks_only_what_head_adds_to_it(
    fixture_product: Path, monkeypatch: pytest.MonkeyPatch, commit: Callable[[Path, str], None]
) -> None:
    monkeypatch.setenv("CI", "")
    subprocess.run(["git", "checkout", "-q", "-b", "work"], cwd=fixture_product, check=True)
    notes = fixture_product / "app" / "notes.py"
    notes.write_text(notes.read_text() + "\n\nLIMIT = 500\n")
    commit(fixture_product, "a limit")
    first = subprocess.run(["git", "rev-parse", "HEAD"], cwd=fixture_product, capture_output=True, text=True,
                           check=True).stdout.strip()  # fmt: skip
    (fixture_product / "qa" / "scenarios" / "README.md").write_text("# QA scenarios\n\nEach opens with its labels.\n")
    commit(fixture_product, "words")

    assert "over 2 changed path(s)" in check(fixture_product, "--only", "qa-scenarios")[1]
    assert "over 1 changed path(s)" in check(fixture_product, "--only", "qa-scenarios", "--since", first)[1]


def test_an_uncommitted_change_is_refused(
    fixture_product: Path, monkeypatch: pytest.MonkeyPatch, commit: Callable[[Path, str], None]
) -> None:
    monkeypatch.setenv("CI", "")
    subprocess.run(["git", "checkout", "-q", "-b", "work"], cwd=fixture_product, check=True)
    (fixture_product / "qa" / "scenarios" / "README.md").write_text("# QA scenarios\n\nEach opens with its labels.\n")
    status, output = check(fixture_product)
    assert status == 2 and "commit your changes first" in output


@pytest.mark.parametrize(
    ("script", "said"),
    [("sleep 5", "passed 1 s, so it was stopped"), ('echo "per test: $FACTORY_TEST_SECONDS"; exit 1', "per test: 45")],
    ids=["hung-run", "per-test-limit-given"],
)
def test_a_test_run_on_the_laptop_has_its_time_limits(make_repo: Callable[..., Path], script: str, said: str) -> None:
    root = make_repo({"scripts/dev*": f"#!/bin/sh\n{script}\n"})
    repo = Repo.open(root)
    repo.test_seconds = (45, 1)
    values = {"always": [], "full": [], "skip_suffixes": [], "readers": {}}

    problems = tests_check.run(repo, values)
    assert len(problems) == 1 and said in problems[0]


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
