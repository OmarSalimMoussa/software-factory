"""Complexity ratchet: no Python file's complexity grows.

Per file, the summed excess over ruff's C901 (cyclomatic complexity), PLR0911 (returns), PLR0912 (branches) and
PLR0915 (statements) limits: how far past each limit, not how many functions pass one, so splitting a function never
reads as a regression. Measured by the factory's pinned ruff; a per-file ratchet in check-values/baseline.json.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from checks.runner import budgets, scope, tools
from checks.runner.repo import Repo
from checks.runner.values import RATCHET_STANZA

ID = "complexity-ratchet"
RULE = "No file's complexity grows"
VALUES_FILE = None
VALUES = None

STANZA = "complexity"
SELECT = "C901,PLR0911,PLR0912,PLR0915"
REMEDY = "split the function by concern"
_OVER_RE = re.compile(r"\((\d+) > (\d+)\)")


class MeasureError(Exception):
    """Ruff could not measure, or answered in a shape this check cannot read."""


def excess(message: str) -> int:
    """How far one violation is past its limit, read from ruff's ``(measured > limit)``."""
    match = _OVER_RE.search(message)
    if match is None:
        raise MeasureError(f"ruff's message has no '(measured > limit)': {message!r}")
    return int(match.group(1)) - int(match.group(2))


def measure(repo: Repo) -> dict[str, int]:
    paths = sorted(rel for rel in repo.tracked if rel.endswith(".py") and scope.classify(rel) in ("source", "tests"))
    counts: Counter[str] = Counter({rel: 0 for rel in paths})
    if not paths:
        return dict(counts)
    # --isolated: the standard limits, whatever the product's own ruff settings say.
    done = subprocess.run(
        [str(tools.binary("ruff")), "check", "--isolated", "--select", SELECT, "--output-format", "json", *paths],
        cwd=repo.root,
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode not in (0, 1):
        raise MeasureError(f"ruff exited {done.returncode}: {done.stderr.strip()}")
    violations = json.loads(done.stdout)
    root = repo.root.resolve()
    for entry in violations:
        rel = Path(entry["filename"]).resolve().relative_to(root).as_posix()
        counts[rel] += excess(str(entry["message"]))
    return dict(counts)


def run(repo: Repo, values: dict[str, Any]) -> list[str]:
    problems = budgets.check_ratchet(STANZA, measure(repo), repo.stanza(STANZA, RATCHET_STANZA))
    return [f"{p}; {REMEDY}" for p in problems]
