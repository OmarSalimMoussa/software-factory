"""The Python files a product's ruff configuration excludes. They are not code, so no check parses or measures them.

The product names them under ``[tool.ruff]`` in pyproject.toml, as ``exclude`` and ``extend-exclude``. The factory's
pinned ruff applies those two settings alone (``--isolated``), with ``--force-exclude``'s reading of them, so the
product's other ruff settings, perhaps written for a newer ruff, never stop it.
"""

from __future__ import annotations

import json
import subprocess
import tomllib
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from checks.runner import tools

KEYS = ("exclude", "extend-exclude")
SUFFIXES = (".py", ".pyi")


class ExcludeError(Exception):
    """The product's ruff exclusions cannot be read, or ruff cannot apply them."""


def _ruff_table(root: Path) -> dict[str, Any]:
    path = root / "pyproject.toml"
    if not path.is_file():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ExcludeError(f"pyproject.toml cannot be read for ruff's exclusions: {exc}") from exc
    tool = data.get("tool", {})
    table = tool.get("ruff", {}) if isinstance(tool, dict) else None
    if not isinstance(table, dict):
        raise ExcludeError("pyproject.toml: [tool] and [tool.ruff] must be tables")
    return table


def ruff_args(root: Path) -> list[str]:
    """What makes an isolated ruff skip every file the product's configuration excludes."""
    table = _ruff_table(root)
    args = ["--force-exclude"]
    for key in KEYS:
        if key not in table:
            continue
        patterns = table[key]
        if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
            raise ExcludeError(f"pyproject.toml: [tool.ruff] {key} must be a list of patterns")
        # A JSON list of strings is also a TOML array, which --config reads as one setting.
        args += ["--config", f"{key} = {json.dumps(patterns)}"]
    return args


def excluded(root: Path, tracked: Iterable[str]) -> frozenset[str]:
    """The tracked Python files ruff skips under the product's exclusions."""
    python = sorted(rel for rel in tracked if rel.endswith(SUFFIXES))
    if not python:
        return frozenset()
    done = subprocess.run(
        [str(tools.binary("ruff")), "check", "--isolated", *ruff_args(root), "--show-files", *python],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        raise ExcludeError(f"ruff could not list the files it reads (exit {done.returncode}): {done.stderr[-300:]}")
    base = root.resolve()
    try:
        kept = {Path(line).relative_to(base).as_posix() for line in done.stdout.splitlines() if line.strip()}
    except ValueError as exc:
        raise ExcludeError(f"ruff listed a file outside the product: {exc}") from exc
    return frozenset(rel for rel in python if rel not in kept)
