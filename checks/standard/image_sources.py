"""Image sources: a Dockerfile that copies single source files copies everything they import.

A Dockerfile that copies a few TypeScript files out of a sibling tree compiles in the checkout, where every file is
present, and fails in the image once a copied file imports one the Dockerfile does not copy. Every relative import is
followed from what each listed image copies (files, and folders without their tests), and one that lands outside the
copied set fails with the ``COPY`` line to add. Each image is listed with its build context.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from checks.runner.repo import Repo

ID = "image-sources"
RULE = "A Dockerfile that copies single source files copies what they import"
VALUES_FILE = "image-sources.toml"
VALUES = {"images": [{"dockerfile": str, "context": str}]}

_SKIPPED_PARTS = frozenset({"node_modules", "dist"})
_COPY = re.compile(r"^COPY\s+(?!--)(.+)$", re.MULTILINE)
_IMPORT = re.compile(r"""(?:import|export)[^'"]*?from\s+['"](\.[^'"]+)['"]""")


def copied(dockerfile_text: str, context: Path) -> tuple[set[Path], set[Path]]:
    """The files and the folders the Dockerfile copies from ``context``."""
    files: set[Path] = set()
    dirs: set[Path] = set()
    for match in _COPY.finditer(dockerfile_text):
        for source in match.group(1).split()[:-1]:
            path = (context / source).resolve()
            (dirs if source.endswith("/") or path.is_dir() else files).add(path)
    return files, dirs


def _target(importer: Path, spec: str) -> Path | None:
    """The file a relative import names, as TypeScript resolves it; None when it names none."""
    base = importer.parent / spec
    if spec.endswith(".js"):
        candidates = [base.with_suffix(".ts")]
    elif base.suffix == ".ts":
        candidates = [base]
    else:
        candidates = [Path(f"{base}.ts"), base / "index.ts"]
    return next((one.resolve() for one in candidates if one.is_file()), None)


def _sources(files: set[Path], dirs: set[Path]) -> list[Path]:
    found = [path for path in files if path.suffix == ".ts"]
    for folder in dirs:
        found += [path for path in folder.rglob("*.ts")
                  if not path.name.endswith(".test.ts") and _SKIPPED_PARTS.isdisjoint(path.parts)]  # fmt: skip
    return sorted(found)


def image_problems(dockerfile: str, text: str, context: Path) -> list[str]:
    context = context.resolve()
    files, dirs = copied(text, context)
    todo = _sources(files, dirs)
    seen: set[Path] = set()
    problems: list[str] = []
    while todo:
        source = todo.pop()
        if source in seen:
            continue
        seen.add(source)
        shown = os.path.relpath(source, context)
        for spec in _IMPORT.findall(source.read_text(encoding="utf-8")):
            target = _target(source, spec)
            if target is None:
                problems.append(f"{ID}: {dockerfile}: {shown} imports {spec}, which is no file")
                continue
            if target not in files and not any(one in target.parents for one in dirs):
                missing = os.path.relpath(target, context)
                problems.append(f"{ID}: {dockerfile}: {shown} imports {spec}, which the image does not copy. "
                                f"Add: COPY {missing} {missing}")  # fmt: skip
            todo.append(target)
    return problems


def run(repo: Repo, values: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for image in values["images"]:
        dockerfile, context = image["dockerfile"], repo.root / image["context"]
        if not (repo.root / dockerfile).is_file():
            problems.append(f"{ID}: {dockerfile} is missing")
        elif not context.is_dir():
            problems.append(f"{ID}: {dockerfile}: its context {image['context']} is no folder")
        else:
            problems += image_problems(dockerfile, repo.read(dockerfile), context)
    return problems
