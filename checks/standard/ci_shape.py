"""CI shape: the product's CI is the factory's template, file for file, and the product's own work lives in the four
``scripts/ci/`` steps it calls.

The templates are read from the factory this check runs in, so a product is held to that commit's copy. Other
workflows may sit beside the two. ``.github/factory-ref`` pins the factory repo and commit the CI's checks run from.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from checks.runner.repo import Repo
from checks.standard.repo_shape import executable
from runtime.shared.paths import PROMOTE_WORKFLOW

ID = "ci-shape"
RULE = "The CI is the factory's template, pinned to a factory commit, with the four scripts/ci steps"
VALUES_FILE = None
VALUES = None

TEMPLATES = Path(__file__).resolve().parents[2] / "templates" / "ci"
WORKFLOWS = ("ci.yml", PROMOTE_WORKFLOW)
STEPS = ("build", "smoke", "deploy-staging", "promote")
FACTORY_REF = ".github/factory-ref"
_REF = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}")


def _first_difference(mine: bytes, template: bytes) -> int:
    ours, theirs = mine.splitlines(keepends=True), template.splitlines(keepends=True)
    pairs = enumerate(zip(ours, theirs, strict=False), 1)
    return next((n for n, (a, b) in pairs if a != b), min(len(ours), len(theirs)) + 1)


def workflow_problems(repo: Repo) -> list[str]:
    out = []
    for name in WORKFLOWS:
        rel = f".github/workflows/{name}"
        if rel not in repo.tracked:
            out.append(f"{rel} is missing (or not committed): copy the factory's templates/ci/{name} there unchanged")
            continue
        mine, template = (repo.root / rel).read_bytes(), (TEMPLATES / name).read_bytes()
        if mine != template:
            out.append(
                f"{rel} differs from the factory's template at line {_first_difference(mine, template)}: copy "
                f"templates/ci/{name} over it unchanged; the product's own work goes in scripts/ci/"
            )
    return out


def ref_problems(repo: Repo) -> list[str]:
    wanted = "one line, owner/name@<40-character sha>, naming the factory repo and commit CI checks with"
    if FACTORY_REF not in repo.tracked:
        return [f"{FACTORY_REF} is missing (or not committed): write {wanted}"]
    lines = repo.read(FACTORY_REF).splitlines()
    if len(lines) != 1 or not _REF.fullmatch(lines[0]):
        return [f"{FACTORY_REF} must hold {wanted}; it holds {repo.read(FACTORY_REF)[:80]!r}"]
    return []


def run(repo: Repo, values: dict[str, Any]) -> list[str]:
    steps = [problem for step in STEPS for problem in executable(repo, f"scripts/ci/{step}")]
    return workflow_problems(repo) + ref_problems(repo) + steps
