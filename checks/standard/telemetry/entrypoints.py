"""Entrypoint discovery and the boot check.

An entrypoint is a Python file with a ``__name__`` guard, a ``python -m`` or ``node`` target of a Dockerfile ``CMD``
or ``ENTRYPOINT``, or a ``.ts`` input a listed vite config resolves. Each needs a processes row (and then must make
its boot call) or a non-booting row. A row no entrypoint answers is stale.
"""

from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from checks.standard.telemetry.config import WHERE, BootCall, Registry
from checks.standard.telemetry.facts import FileFacts
from checks.standard.telemetry.python_scan import Resolver

_INSTRUCTION = re.compile(r"^\s*(CMD|ENTRYPOINT)\s+(.*)$", re.I)
_VITE_INPUT = re.compile(r"resolve\(\s*__dirname\s*,\s*'([^']+\.ts)'\s*\)")


def _argv(value: str) -> list[str]:
    value = value.strip()
    if value.startswith("["):
        return [str(part) for part in json.loads(value)]
    return value.split()


def _docker_targets(dockerfile: str, text: str, resolver: Resolver, registry: Registry) -> tuple[set[str], list[str]]:
    found: set[str] = set()
    problems: list[str] = []
    base = posixpath.dirname(dockerfile)
    for number, line in enumerate(text.splitlines(), 1):
        match = _INSTRUCTION.match(line)
        if not match:
            continue
        try:
            argv = _argv(match.group(2))
        except ValueError:
            problems.append(f"{dockerfile}:{number} — its exec form is not a JSON list")
            continue
        for i, word in enumerate(argv[:-1]):
            if word == "-m" and "python" in " ".join(argv[:i]):
                target = resolver.module(dockerfile, argv[i + 1], 0)
                if target is None:
                    problems.append(
                        f"{dockerfile}:{number} — `python -m {argv[i + 1]}` resolves to no tracked first-party module"
                    )
                else:
                    found.add(target)
            if posixpath.basename(word) == "node":
                dist = posixpath.normpath(posixpath.join(base, argv[i + 1]))
                if dist in registry.dist_to_src:
                    found.add(registry.dist_to_src[dist])
                else:
                    problems.append(
                        f"{dockerfile}:{number} — `node {argv[i + 1]}` has no dist_to_src row in {WHERE} mapping it "
                        "to its source file"
                    )
    return found, problems


def discover(
    root: Path,
    files: Sequence[tuple[str, str]],
    facts: Mapping[str, FileFacts],
    resolver: Resolver,
    registry: Registry,
) -> tuple[set[str], list[str]]:
    """Every entrypoint among ``files`` (each a path and its language) and the problems met finding them."""
    entries = {path for path, f in facts.items() if f.main_guard}
    problems: list[str] = []
    for path, language in files:
        if language == "dockerfile":
            found, bad = _docker_targets(path, (root / path).read_text(encoding="utf-8"), resolver, registry)
            entries |= found
            problems += bad
    for path in sorted(registry.vite_configs):
        config = root / path
        if not config.is_file():
            problems.append(f"{WHERE}: vite_configs names {path}, which is no tracked file")
            continue
        base = posixpath.dirname(path)
        text = config.read_text(encoding="utf-8")
        entries |= {posixpath.normpath(posixpath.join(base, rel)) for rel in _VITE_INPUT.findall(text)}
    return entries, problems


def _answers(row: str, path: str) -> bool:
    return path == row or (row.endswith("/") and path.startswith(row))


def check(entries: set[str], facts: Mapping[str, FileFacts], registry: Registry) -> list[str]:
    problems: list[str] = []
    booting = {path for path in registry.processes if path in entries}
    for path in sorted(entries):
        if path in registry.processes:
            continue
        if not any(_answers(row, path) for row in registry.non_booting):
            problems.append(
                f"entrypoint {path} has no processes or non_booting row — add one to {WHERE} naming the process it "
                "boots (and its boot call), or why it boots none"
            )
    problems += [
        f"stale processes row {path}: no entrypoint answers it — remove it from {WHERE}"
        for path in sorted(set(registry.processes) - booting)
    ]
    problems += [
        f"stale non_booting row {row}: no entrypoint answers it — remove it from {WHERE}"
        for row in sorted(registry.non_booting)
        if not any(_answers(row, path) for path in entries)
    ]
    for path in sorted(booting):
        process, boot = registry.processes[path]
        problems += _boot_problems(path, process, boot, facts.get(path))
    return problems


def _boot_problems(path: str, process: str, boot: BootCall, facts: FileFacts | None) -> list[str]:
    calls = facts.calls if facts is not None else ()
    if any(
        c.name == boot.name
        and c.receiver == boot.receiver
        and (boot.first_arg is None or c.first_arg == boot.first_arg)
        for c in calls
    ):
        return []
    return [
        f"{path} boots the {process} process but never calls {boot.shown()} — without it its errors reach no "
        "error tracker"
    ]
