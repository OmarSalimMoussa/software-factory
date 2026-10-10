from __future__ import annotations

from pathlib import Path

FLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"


def test_the_factorys_own_ci_runs_its_tests_lint_and_types() -> None:
    text = (FLOWS / "ci.yml").read_text(encoding="utf-8")

    for command in ("pytest", "ruff check", "ruff format --check", "mypy"):
        assert f"--extra dev {command}" in text
