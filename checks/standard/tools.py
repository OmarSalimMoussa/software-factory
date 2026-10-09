"""Tools: ruff format and check, mypy and import-linter, at the versions the factory pins.

The product configures them in its own pyproject.toml. Ruff runs over the changed Python files (every tracked one with
--all); mypy runs over the files its ``[tool.mypy]`` names, and import-linter over the contracts its
``[tool.importlinter]`` holds, when the product configures them. A product with a ``.venv`` lends mypy its packages.
"""

from __future__ import annotations

import subprocess
import tomllib
from typing import Any

from checks.runner import tools
from checks.runner.repo import Repo

ID = "tools"
RULE = "ruff format and check, mypy, import-linter, at versions the factory pins"
VALUES_FILE = None
VALUES = None


def _run(repo: Repo, argv: list[str]) -> tuple[int, str]:
    done = subprocess.run(argv, cwd=repo.root, capture_output=True, text=True, check=False)
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


def run(repo: Repo, values: dict[str, Any]) -> list[str]:
    try:
        table = config(repo)
    except ValueError as exc:
        return [str(exc)]
    scope = repo.tracked if repo.changed is None else [rel for rel in repo.changed if rel in repo.tracked]
    python = sorted(rel for rel in scope if rel.endswith((".py", ".pyi")))
    ruff = str(tools.binary("ruff"))
    steps: list[tuple[str, list[str]]] = []
    if python:
        steps.append(("ruff format", [ruff, "format", "--check", "--force-exclude", *python]))
        steps.append(("ruff check", [ruff, "check", "--force-exclude", *python]))
    if "mypy" in table:
        venv = repo.root / ".venv" / "bin" / "python"
        lend = ["--python-executable", str(venv)] if venv.is_file() else []
        steps.append(("mypy", [str(tools.binary("mypy")), *lend]))
    if "importlinter" in table:
        steps.append(("import-linter", [str(tools.binary("lint-imports")), "--no-cache"]))
    problems: list[str] = []
    for name, argv in steps:
        status, output = _run(repo, argv)
        if status != 0:
            problems.append(f"{name} failed (exit {status}):\n{output}")
    return problems
