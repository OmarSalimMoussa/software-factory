"""Tools: ruff format and check, mypy and import-linter, at the versions the product pins.

The product configures them in its own pyproject.toml. A product with a ``uv.lock`` has its environment synced first
(``uv sync --frozen --all-extras``), and each tool then runs from its ``.venv/bin/``, where mypy also finds the
product's packages and plugins. A tool the product does not pin there runs at the factory's pinned version. Ruff runs
over the changed Python files (every tracked one with --all); mypy runs over the files its ``[tool.mypy]`` names,
and import-linter over the contracts its ``[tool.importlinter]`` holds, when the product configures them.
"""

from __future__ import annotations

import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

from checks.runner import tools
from checks.runner.repo import Repo
from runtime.shared import environment

ID = "tools"
RULE = "ruff format and check, mypy, import-linter, at the versions the product pins"
VALUES_FILE = None
VALUES = None

LOCK = "uv.lock"
SYNC = ("sync", "--frozen", "--all-extras", "--quiet")
# Either would point uv at another environment than the product's .venv, where the tools are taken from.
_ELSEWHERE = ("UV_PROJECT_ENVIRONMENT", "VIRTUAL_ENV")


def _run(repo: Repo, argv: list[str], env: dict[str, str] | None = None) -> tuple[int, str]:
    done = subprocess.run(argv, cwd=repo.root, capture_output=True, text=True, check=False, env=env)
    return done.returncode, (done.stdout + done.stderr).strip()


def config(repo: Repo) -> dict[str, Any]:
    """The product's ``[tool]`` table; raises ValueError naming the problem."""
    path = repo.root / "pyproject.toml"
    if not path.is_file():
        raise ValueError("pyproject.toml is missing: it configures the tools")
    try:
        table = tomllib.loads(path.read_text(encoding="utf-8")).get("tool", {})
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"pyproject.toml does not parse: {exc}") from exc
    if not isinstance(table, dict):
        raise ValueError("pyproject.toml: [tool] must be a table")
    return table


def sync(repo: Repo) -> str:
    """Sync the product's environment from its lock; the problem, or "" when it synced or there is no lock."""
    if not (repo.root / LOCK).is_file():
        return ""
    uv = shutil.which("uv")
    if uv is None:
        return f"uv is not on PATH, so the product's {LOCK} cannot be synced and its own tools cannot run"
    env = {key: value for key, value in environment.handed_on().items() if key not in _ELSEWHERE}
    status, output = _run(repo, [uv, *SYNC], env)
    return f"uv sync failed (exit {status}), so the product's own tools cannot run:\n{output}" if status else ""


def _own(repo: Repo, name: str) -> Path | None:
    path = repo.root / ".venv" / "bin" / name
    return path if path.is_file() else None


def binary(repo: Repo, name: str) -> str:
    """The product's own copy of a tool, or the factory's pinned one when the product pins none."""
    return str(_own(repo, name) or tools.binary(name))


def run(repo: Repo, values: dict[str, Any]) -> list[str]:
    try:
        table = config(repo)
    except ValueError as exc:
        return [str(exc)]
    failed = sync(repo)
    if failed:
        return [failed]
    scope = repo.tracked if repo.changed is None else [rel for rel in repo.changed if rel in repo.tracked]
    python = sorted(rel for rel in scope if rel.endswith((".py", ".pyi")))
    ruff = binary(repo, "ruff")
    steps: list[tuple[str, list[str]]] = []
    if python:
        steps.append(("ruff format", [ruff, "format", "--check", "--force-exclude", *python]))
        steps.append(("ruff check", [ruff, "check", "--force-exclude", *python]))
    if "mypy" in table:
        # The factory's mypy runs under the factory's Python: it is lent the product's, to find the product's packages.
        venv = _own(repo, "python")
        lend = ["--python-executable", str(venv)] if venv is not None and _own(repo, "mypy") is None else []
        steps.append(("mypy", [binary(repo, "mypy"), *lend]))
    if "importlinter" in table:
        steps.append(("import-linter", [binary(repo, "lint-imports"), "--no-cache"]))
    problems: list[str] = []
    for name, argv in steps:
        status, output = _run(repo, argv)
        if status != 0:
            problems.append(f"{name} failed (exit {status}):\n{output}")
    return problems
