"""The product checkout a check reads: its root, its tracked files, what the branch changed, and its values.

Checks read the working tree of tracked files only (an untracked file is invisible until it is added), and their values
only through ``values`` and ``stanza``, so a check never holds a fact about any one product.
"""

from __future__ import annotations

import json
import subprocess
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from checks.runner import excludes
from checks.runner import values as values_mod
from checks.runner.values import BASELINE, VALUES_DIR, Schema, ValuesError


class GitError(Exception):
    """A git command the checks need failed; the message names it and what git said."""


def git(root: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {done.stderr.strip() or done.stdout.strip()}")
    return done.stdout


def tracked_files(root: Path) -> tuple[str, ...]:
    listed = git(root, "ls-files", "-z").split("\0")
    # A conflicted path is listed once per merge stage; a tracked symlink to a directory is no file to read.
    return tuple(rel for rel in dict.fromkeys(listed) if rel and (root / rel).is_file())


@dataclass
class Repo:
    root: Path
    tracked: tuple[str, ...]
    changed: tuple[str, ...] | None  # None when checking everything
    # (ceiling MiB, reserve MiB) a test run must fit in, from the budget.test_* settings; None where nothing else
    # shares the machine (CI), so no guard is needed.
    test_memory: tuple[int, int] | None = None
    _baseline: dict[str, Any] | None = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _excluded: frozenset[str] | None = field(default=None, repr=False)
    _excluded_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def open(cls, root: Path, changed: Sequence[str] | None = None) -> Repo:
        return cls(root, tracked_files(root), None if changed is None else tuple(changed))

    def read(self, rel: str) -> str:
        return (self.root / rel).read_text(encoding="utf-8", errors="replace")

    def git(self, *args: str) -> str:
        return git(self.root, *args)

    def excluded(self, rel: str) -> bool:
        """Whether ``rel`` is Python the product's ruff configuration excludes: no check parses or measures it."""
        if not rel.endswith(excludes.SUFFIXES):
            return False
        with self._excluded_lock:
            if self._excluded is None:
                self._excluded = excludes.excluded(self.root, self.tracked)
        return rel in self._excluded

    def values(self, name: str, schema: Schema) -> dict[str, Any]:
        return values_mod.load(self.root, name, schema)

    def stanza(self, name: str, schema: Schema) -> dict[str, Any]:
        """One stanza of check-values/baseline.json, checked against ``schema``."""
        with self._lock:
            if self._baseline is None:
                self._baseline = values_mod.load(self.root, BASELINE, {str: dict})
        if name not in self._baseline:
            raise ValuesError([f"{VALUES_DIR}/{BASELINE} has no {name!r} stanza: add it (an empty one is allowed)"])
        found = values_mod.problems(schema, self._baseline[name], f"{VALUES_DIR}/{BASELINE}:{name}")
        if found:
            raise ValuesError(found)
        stanza: dict[str, Any] = self._baseline[name]
        return stanza

    def head_stanza(self, name: str) -> dict[str, Any]:
        """The stanza as committed at HEAD, or empty when HEAD has none (a new product, a new stanza)."""
        done = subprocess.run(
            ["git", "show", f"HEAD:{VALUES_DIR}/{BASELINE}"], cwd=self.root, capture_output=True, text=True, check=False
        )
        if done.returncode != 0:
            return {}
        try:
            stanza = json.loads(done.stdout).get(name, {})
        except (ValueError, AttributeError):
            return {}
        return stanza if isinstance(stanza, dict) else {}
