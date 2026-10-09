"""The check tools the factory pins (ruff, mypy, import-linter), found beside the interpreter running the checks.

`factory check` runs under the factory's tools environment, so a tool missing there is an install fault, reported
as one, never a silent skip.
"""

from __future__ import annotations

import sys
from pathlib import Path


class ToolMissing(Exception):
    """A pinned tool is not installed beside the interpreter running the checks."""


def binary(name: str) -> Path:
    path = Path(sys.executable).parent / name
    if not path.is_file():
        raise ToolMissing(
            f"{name} is not installed beside {sys.executable}: run factory check from the factory's "
            "tools environment (the installer puts it beside the install)"
        )
    return path
