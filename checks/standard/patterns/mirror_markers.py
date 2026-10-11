"""Mirror markers: every declared mirror is claimed by a group, a test, recorded debt or a reason.

A comment that declares a hand-kept mirror in one of the standard phrases ("must mirror", "keep in sync", ...) is a
promise nothing keeps unless a claim says how it is covered: ``group:<name>`` (a mirrored-constants group),
``test:<path>`` (a test that compares the copies), ``debt:<reason>`` or ``not-a-mirror:<reason>``. A server vocabulary
copied into browser code with no comment at all is found by structure and must be covered the same way. Both checks
run both ways: an unclaimed declaration fails, and a claim or copy row that matches nothing fails as stale. A mirror
declared in other words is invisible here.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from checks.runner import scope
from checks.runner.repo import Repo
from checks.runner.values import ValuesError
from checks.standard.patterns import copies, engine, mirrored_constants

ID = "mirror-markers"
RULE = 'Every "must mirror" note is claimed by a test, a group, recorded debt or a reason'
VALUES_FILE = "mirror-markers.toml"
CLAIM = {"path": str, "phrase": str, "fragment": str, "covered_by": str}
COPY = {"path": str, "name": str, "covered_by": str}
VALUES = {
    "skip": [str],
    "claims": [CLAIM],
    "copies": {"server": [str], "web": [str], "generated_markers": [str], "rows": [COPY]},
}
PHRASES = (
    "must mirror", "must match the", "change both", "edit every copy together", "byte-identical across",
    "byte-identical to", "byte-equal to", "keep in sync", "kept in sync", "contract with", "mirrored by",
    "in lockstep",
)  # fmt: skip
SUFFIXES = (".py", ".ts", ".sh", ".mjs", ".css", ".html", ".tsx", ".template", ".js", ".rs", ".yml", ".yaml", ".md",
            "Dockerfile")  # fmt: skip
_PHRASE_RE = re.compile("|".join(re.escape(p) for p in PHRASES), re.I)
_NEVER = (*scope.DATED_RECORDS, "check-values/")


def coverage_problem(repo: Repo, label: str, covered_by: str, groups: Sequence[str]) -> str | None:
    kind, _, detail = covered_by.partition(":")
    if not detail.strip():
        return f"{label}: covered_by needs a detail after the colon"
    if kind == "group" and detail not in groups:
        return f"{label}: no mirrored-constants group is named {detail!r}"
    if kind == "test" and not (repo.root / detail).is_file():
        return f"{label}: the covering test {detail} does not exist"
    if kind not in ("group", "test", "debt", "not-a-mirror"):
        return f"{label}: covered_by must start with group:, test:, debt: or not-a-mirror:"
    return None


def declarations(repo: Repo, skip: Sequence[str]) -> list[tuple[str, int, str, str]]:
    """Every (path, line, phrase, text) that declares a mirror, in tracked order."""
    hits: list[tuple[str, int, str, str]] = []
    for rel in repo.tracked:
        if not rel.endswith(SUFFIXES) or scope.vendored(rel) or scope.under(rel, (*_NEVER, *skip)):
            continue
        for number, line in enumerate(repo.read(rel).splitlines(), 1):
            match = _PHRASE_RE.search(line)
            if match:
                hits.append((rel, number, match.group(0).lower(), line.strip()))
    return hits


def _claims(hit: tuple[str, int, str, str], claim: dict[str, str]) -> bool:
    rel, _, phrase, text = hit
    return claim["path"] == rel and claim["phrase"] == phrase and claim["fragment"].lower() in text.lower()


def _groups(repo: Repo) -> tuple[list[str], list[dict[str, Any]], list[str]]:
    try:
        groups = repo.values(mirrored_constants.VALUES_FILE, mirrored_constants.VALUES)["groups"]
    except ValuesError as exc:
        return [], [], [f"{ID}: claims are checked against the groups, which cannot be read: {p}" for p in exc.problems]
    return [g["name"] for g in groups], groups, []


def _copy_problems(repo: Repo, settings: dict[str, Any], groups: list[dict[str, Any]]) -> list[str]:
    try:
        found = copies.find(repo, settings["server"], settings["web"], settings["generated_markers"])
    except SyntaxError as exc:
        return [f"{ID}: server Python does not parse, so its vocabularies cannot be read: {exc}"]
    rows = {(row["path"], row["name"]) for row in settings["rows"]}

    def group_covers(copy: copies.Found) -> bool:
        return any(s["path"] == copy.web_path and copy.web_name in s["pattern"] for g in groups for s in g["sides"])

    first: dict[tuple[str, str], copies.Found] = {}
    for copy in found:
        first.setdefault((copy.web_path, copy.web_name), copy)
    problems = [
        f"{c.web_path}:{c.web_line}: {c.web_name} copies {c.py_path}:{c.py_line} {c.py_name} — send it in the "
        "payload, or pin it with a mirrored-constants group (kind set), or add a copies row saying how it is covered"
        for c in first.values()
        if not group_covers(c) and (c.web_path, c.web_name) not in rows
    ]
    return problems + [f"{ID}: stale copy row: {path} {name} matches no copy; remove it"
                       for path, name in sorted(rows - set(first))]  # fmt: skip


def run(repo: Repo, values: dict[str, Any]) -> list[str]:
    names, groups, problems = _groups(repo)
    problems += engine.missing(repo, ID, [*values["copies"]["server"], *values["copies"]["web"]])
    claims, rows = values["claims"], values["copies"]["rows"]
    for row in [*claims, *rows]:
        if "phrase" in row and row["phrase"] not in PHRASES:
            problems.append(f"{ID}: claim for {row['path']}: {row['phrase']!r} is not a standard phrase")
        found = coverage_problem(repo, f"{ID}: row for {row['path']}", row["covered_by"], names)
        problems += [found] if found else []
    if problems:
        return problems
    hits = declarations(repo, values["skip"])
    problems += [
        f"{rel}:{line}: declared mirror is not claimed: {text!r} — add a mirrored-constants group and claim it, "
        "cite the test that covers it, or record it as debt with a reason"
        for rel, line, phrase, text in hits
        if not any(_claims((rel, line, phrase, text), c) for c in claims)
    ]
    problems += [
        f"{ID}: stale claim: {c['path']} ({c['phrase']!r}, fragment {c['fragment']!r}) matches no declaration; "
        "remove it"
        for c in claims
        if not any(_claims(hit, c) for hit in hits)
    ]
    return problems + _copy_problems(repo, values["copies"], groups)
