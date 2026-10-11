"""Complexity ratchet: no Python file's complexity grows.

Per file, the summed excess over ruff's C901 (cyclomatic complexity), PLR0911 (returns), PLR0912 (branches) and
PLR0915 (statements) limits: how far past each limit, not how many functions pass one, so splitting a function never
reads as a regression. Measured by the factory's pinned ruff, which skips what the product's ruff configuration
excludes; a file that does not parse is one problem naming it. A per-file ratchet against the default branch: each
file the branch changed, measured again as it stood there.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from checks.runner import budgets, excludes, scope, tools
from checks.runner.repo import Repo

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


def _ruff(root: Path, paths: list[str], exclusions: list[str]) -> tuple[dict[str, int], dict[str, str]]:
    """Each file's excess under ``root``, and why each file that does not parse cannot be measured."""
    counts: Counter[str] = Counter({rel: 0 for rel in paths})
    unparsable: dict[str, str] = {}
    if not paths:
        return dict(counts), unparsable
    # --isolated: the standard limits, whatever the product's own ruff settings say, but its exclusions.
    argv = [str(tools.binary("ruff")), "check", "--isolated", *exclusions, "--select", SELECT, "--output-format",
            "json", *paths]  # fmt: skip
    done = subprocess.run(argv, cwd=root, capture_output=True, text=True, check=False)
    if done.returncode not in (0, 1):
        raise MeasureError(f"ruff exited {done.returncode}: {done.stderr.strip()}")
    resolved = root.resolve()
    for entry in json.loads(done.stdout):
        rel = Path(entry["filename"]).resolve().relative_to(resolved).as_posix()
        # Every selected rule has a code; an entry without one is ruff's report that the file does not parse.
        if entry["code"] is None:
            unparsable.setdefault(rel, str(entry["message"]))
        else:
            counts[rel] += excess(str(entry["message"]))
    return {rel: n for rel, n in counts.items() if rel not in unparsable}, unparsable


def measure(repo: Repo) -> tuple[dict[str, int], dict[str, str]]:
    """Each measured file's excess, and why each file that does not parse cannot be measured."""
    paths = sorted(
        rel
        for rel in repo.tracked
        if rel.endswith(".py") and scope.classify(rel) in ("source", "tests") and not repo.excluded(rel)
    )
    return _ruff(repo.root, paths, excludes.ruff_args(repo.root))


def measure_before(repo: Repo, paths: list[str]) -> dict[str, int]:
    """Each of ``paths`` measured as it stood on the default branch; a file it lacked, or that did not parse there,
    is left out."""
    with tempfile.TemporaryDirectory() as scratch:
        root, there = Path(scratch), []
        for rel in paths:
            old = repo.before(rel)
            if old is not None:
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text(old, encoding="utf-8")
                there.append(rel)
        return _ruff(root, there, [])[0]


def run(repo: Repo, values: dict[str, Any]) -> list[str]:
    counts, unparsable = measure(repo)
    broken = [f"{ID}: {rel} does not parse, so its complexity cannot be measured: {why}" for rel, why in
              unparsable.items()]  # fmt: skip
    if repo.changed is None:
        return broken  # a ratchet compares a branch's change with the default branch
    changed = {rel: n for rel, n in counts.items() if rel in repo.changed}
    problems = budgets.check_ratchet(STANZA, changed, measure_before(repo, sorted(changed)))
    return [f"{p}; {REMEDY}" for p in problems] + broken
