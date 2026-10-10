"""Stages the deploy image's build context: the files a deploy needs, the plan and the commit, for CI to build.

``python3 -m standard.deploy_image --repo . --plan plan.json --out <dir>`` writes ``<dir>/Dockerfile`` (an image
FROM scratch holding ``/deploy/``) and ``<dir>/deploy/`` with each plan file at its repo path (a folder copied
whole), ``plan.json`` and ``COMMIT`` (the full sha). A file the plan names that is missing, or any link, is an
error: the server copies the folder out as it is, so a link could point anywhere on it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

DOCKERFILE = "FROM scratch\nCOPY deploy/ /deploy/\n"
PLAN = "plan.json"
COMMIT = "COMMIT"
_SHA = re.compile(r"^[0-9a-f]{40}$")


class StageError(Exception):
    """The deploy image cannot be staged; ``problems`` lists every reason."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


def _plan_files(plan: Any) -> tuple[str, list[str]]:
    """The plan's commit and files, or StageError naming what is wrong with it."""
    if not isinstance(plan, dict):
        raise StageError(["the plan is not a JSON object"])
    commit, files = plan.get("commit"), plan.get("files")
    problems = []
    if not isinstance(commit, str) or not _SHA.match(commit):
        problems.append("the plan's commit is not a 40-character sha")
    if not isinstance(files, list) or not files or not all(isinstance(f, str) and f for f in files):
        problems.append("the plan's files must be a non-empty list of paths")
    if problems:
        raise StageError(problems)
    assert isinstance(commit, str) and isinstance(files, list)
    return commit, files


def _links(path: Path) -> list[Path]:
    if path.is_symlink():
        return [path]
    if not path.is_dir():
        return []
    return [p for p in sorted(path.rglob("*")) if p.is_symlink()]


def stage(repo: Path, plan_file: Path, out: Path) -> list[str]:
    """Write the build context into ``out`` (which must not hold anything yet); returns the paths staged."""
    try:
        raw = plan_file.read_bytes()
        plan = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise StageError([f"{plan_file} cannot be read as JSON: {exc}"]) from exc
    commit, files = _plan_files(plan)
    problems: list[str] = []
    for rel in files:
        path = repo / rel
        parts = Path(rel).parts
        if Path(rel).is_absolute() or ".." in parts:
            problems.append(f"{rel}: must be a path inside the repo")
        elif rel in (PLAN, COMMIT):
            problems.append(f"{rel}: the deploy image keeps that name for itself; move the file")
        elif not path.exists() and not path.is_symlink():
            problems.append(f"{rel}: the plan names it but the repo has no such file or folder")
        else:
            problems += [f"{os.path.relpath(link, repo)}: is a link; deploy files must be real files"
                         for link in _links(path)]  # fmt: skip
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        problems.append(f"{out}: must be a new or empty folder")
    if problems:
        raise StageError(problems)
    deploy = out / "deploy"
    deploy.mkdir(parents=True)
    for rel in files:
        source, target = repo / rel, deploy / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, symlinks=True, dirs_exist_ok=True)
        else:
            shutil.copy2(source, target)
    (deploy / PLAN).write_bytes(raw)
    (deploy / COMMIT).write_text(commit + "\n", encoding="utf-8")
    (out / "Dockerfile").write_text(DOCKERFILE, encoding="utf-8")
    return [*files, PLAN, COMMIT]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m standard.deploy_image", description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        staged = stage(args.repo, args.plan, args.out)
    except StageError as exc:
        print("deploy image: cannot stage it:", file=sys.stderr)
        for problem in exc.problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    for rel in staged:
        print(f"/deploy/{rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
