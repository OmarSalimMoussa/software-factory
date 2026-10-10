from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from checks.runner.repo import Repo
from checks.standard import ci_shape, repo_shape

Break = Callable[[Path], None]


def _edit(rel: str, old: str, new: str) -> Break:
    def apply(root: Path) -> None:
        path = root / rel
        text = path.read_text()
        assert old in text, (rel, old)
        path.write_text(text.replace(old, new))

    return apply


def _chmod(rel: str) -> Break:
    return lambda root: (root / rel).chmod(0o644)


def _add(rel: str, text: str) -> Break:
    def apply(root: Path) -> None:
        (root / rel).write_text(text)

    return apply


def test_the_fixture_has_the_standard_shape(fixture_product: Path) -> None:
    assert repo_shape.run(Repo.open(fixture_product), {}) == []


@pytest.mark.parametrize(
    ("damage", "said"),
    [
        (_edit("GOAL.md", "## How it speaks", "## Voice"), "GOAL.md has no '## How it speaks' heading"),
        (_edit("GOAL.md", "- G2 People", "- G3 People"), "item 2 of the goal is numbered G3"),
        (_edit("GOAL.md", "Done when searching", "Once searching"), "does not read '- G<n> <outcome>. Done when"),
        (_edit(".gitignore", "queue/\n", ""), ".gitignore does not ignore queue/"),
        (_edit("scripts/dev", '"reports")', '"reports", "logs")'), "lists logs, which is not a standard verb"),
        (_edit("scripts/dev", '"verbs", "test",', '"verbs",'), "scripts/dev verbs does not list test"),
        (_chmod("scripts/dev"), "scripts/dev is not executable"),
        (_add("check-values/lint.toml", "x = 1\n"), "check-values/lint.toml feeds no standard check"),
    ],
)
def test_a_broken_shape_fails_naming_the_problem(
    fixture_product: Path, commit: Callable[[Path, str], None], damage: Break, said: str
) -> None:
    damage(fixture_product)
    commit(fixture_product, "damage")
    problems = repo_shape.run(Repo.open(fixture_product), {})
    assert any(said in p for p in problems), problems


def _remove(rel: str) -> Break:
    return lambda root: (root / rel).unlink()


def test_the_fixture_carries_the_factorys_ci(fixture_product: Path) -> None:
    assert ci_shape.run(Repo.open(fixture_product), {}) == []


@pytest.mark.parametrize(
    ("damage", "said"),
    [
        (
            _edit(".github/workflows/ci.yml", "--all", "--all --only tools"),
            ".github/workflows/ci.yml differs from the factory's template at line",
        ),
        (_edit(".github/workflows/deploy-prod.yml", "\n", "\r\n"), "deploy-prod.yml differs from the factory's"),
        (_remove("scripts/ci/smoke"), "scripts/ci/smoke is missing"),
        (_chmod("scripts/ci/promote"), "scripts/ci/promote is not executable"),
        (_add(".github/factory-ref", "owner/software-factory@main\n"), ".github/factory-ref must hold one line"),
    ],
)
def test_a_ci_that_drifts_from_the_template_fails_naming_the_problem(
    fixture_product: Path, commit: Callable[[Path, str], None], damage: Break, said: str
) -> None:
    damage(fixture_product)
    commit(fixture_product, "damage")
    problems = ci_shape.run(Repo.open(fixture_product), {})
    assert any(said in p for p in problems), problems
