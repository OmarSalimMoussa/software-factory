"""The tools check runs the tools at the versions the product pins, from its synced environment."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from checks.runner import tools as factory_tools
from checks.runner.repo import Repo
from checks.standard import tools

MakeRepo = Callable[[dict[str, str]], Path]
PYPROJECT = '[project]\nname = "shop"\n\n[tool.mypy]\nstrict = true\n\n[tool.importlinter]\nroot_package = "app"\n'


def _fake(path: Path, label: str, status: int = 0) -> None:
    """A program that writes its label and arguments to $LOG, then exits ``status``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/sh\necho "{label} $*" >> "$LOG"\necho "{label} said no"\nexit {status}\n')
    path.chmod(0o755)


@pytest.fixture
def product(make_repo: MakeRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A product with a lock and its own ruff and mypy; uv on PATH; the factory's tools in a folder of their own."""
    root = make_repo({"pyproject.toml": PYPROJECT, "uv.lock": "version = 1\n", "app/notes.py": "NOTES = 1\n"})
    for name in ("ruff", "mypy", "python"):
        _fake(root / ".venv" / "bin" / name, f"product-{name}")
    for name in ("ruff", "mypy", "lint-imports"):
        _fake(tmp_path / "factory" / name, f"factory-{name}")
    monkeypatch.setattr(factory_tools, "binary", lambda name: tmp_path / "factory" / name)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin:/bin")
    monkeypatch.setenv("LOG", str(tmp_path / "log"))
    return root


def _ran(product: Path) -> list[str]:
    log = product.parent / "log"
    return log.read_text().splitlines() if log.exists() else []


def test_the_products_pinned_tools_run_after_a_sync_and_the_factorys_only_where_it_pins_none(product: Path) -> None:
    _fake(product.parent / "bin" / "uv", "uv")

    assert tools.run(Repo.open(product), {}) == []
    assert _ran(product) == [
        "uv sync --frozen --all-extras --quiet",
        "product-ruff format --check --force-exclude app/notes.py",
        "product-ruff check --force-exclude app/notes.py",
        "product-mypy ",
        "factory-lint-imports --no-cache",
    ]


def test_a_failed_sync_is_the_checks_problem_and_no_tool_runs(product: Path) -> None:
    _fake(product.parent / "bin" / "uv", "uv", status=2)

    problems = tools.run(Repo.open(product), {})

    assert problems == ["uv sync failed (exit 2), so the product's own tools cannot run:\nuv said no"]
    assert _ran(product) == ["uv sync --frozen --all-extras --quiet"]
