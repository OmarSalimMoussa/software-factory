"""The one engine the pattern checks share: lines that match a rule, in the files it scans, belong only to its owners.

A rule scans tracked files under its folders with its suffixes. A line is a hit when every pattern of one of its rows
matches it and none of its ``unless`` patterns does. An owner is never bound; an allowed file carries grounds and must
answer at least one hit, or it is stale. Tests, test data, vendored trees, dated specs and check-values/ itself are
never scanned: they name the patterns on purpose.
"""

from __future__ import annotations

import io
import re
import tokenize
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from checks.runner import scope
from checks.runner.mask import js_code
from checks.runner.repo import Repo

_TEST_RE = re.compile(
    r"(_test\.(py|mjs|js|rs)$|(^|/)test_[^/]*\.py$|(^|/)(conftest|_testing|_fakes)\.py$"
    r"|\.(test|spec)\.[cm]?[tj]sx?$|(^|/)testUtils/|(^|/)_fixtures\.m?js$|\.d\.ts$)"
)
_NEVER_SCANNED = (*scope.DATED_RECORDS, "check-values/")
_MASKED_TOKENS = {tokenize.STRING, tokenize.COMMENT} | {
    getattr(tokenize, name) for name in ("FSTRING_MIDDLE", "TSTRING_MIDDLE") if hasattr(tokenize, name)
}


def out_of_scope(rel: str) -> bool:
    """Whether no pattern check ever reads ``rel``: a test, test data, vendored, a dated spec or a values file."""
    return (
        bool(_TEST_RE.search(rel)) or scope.in_testdata(rel) or scope.vendored(rel) or scope.under(rel, _NEVER_SCANNED)
    )


def python_masked(text: str) -> list[str]:
    """Python lines with string and comment characters blanked, every column kept; raises SyntaxError."""
    rows = [list(line) for line in text.splitlines()]
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except tokenize.TokenError as exc:
        raise SyntaxError(str(exc)) from exc
    for token in tokens:
        if token.type not in _MASKED_TOKENS:
            continue
        (first, start), (last, end) = token.start, token.end
        for number in range(first, last + 1):
            if number > len(rows):
                break
            row = rows[number - 1]
            lo = start if number == first else 0
            hi = end if number == last else len(row)
            row[lo:hi] = [" "] * (min(hi, len(row)) - lo)
    return ["".join(row) for row in rows]


def lines_of(rel: str, text: str, code_only: bool) -> list[str]:
    """The lines a scan reads: comments and strings blanked when ``code_only`` and the language is known."""
    if code_only and rel.endswith(".py"):
        return python_masked(text)
    if code_only and rel.endswith(scope.TS_SUFFIXES):
        return js_code(text)
    return text.splitlines()


def missing(repo: Repo, label: str, paths: Iterable[str]) -> list[str]:
    """A problem for each listed path that is not in the checkout."""
    return [f"{label}: {rel} does not exist; fix or remove it" for rel in paths if not (repo.root / rel).exists()]


@dataclass(frozen=True)
class Rule:
    label: str  # how problems name the rule
    scans: tuple[str, ...]  # folders or files it reads
    suffixes: tuple[str, ...]  # file endings it reads; empty reads every file
    rows: tuple[tuple[str, ...], ...]  # a line is a hit when every pattern of one row matches
    message: str  # what a hit does, after "path:line: "
    remedy: str
    code_only: bool = True
    unless: tuple[str, ...] = ()
    owners: tuple[str, ...] = ()
    allow: dict[str, str] = field(default_factory=dict)


def _hit(line: str, rows: Sequence[Sequence[re.Pattern[str]]], unless: Sequence[re.Pattern[str]]) -> bool:
    if any(p.search(line) for p in unless):
        return False
    return any(all(p.search(line) for p in row) for row in rows)


def files_of(repo: Repo, scans: Sequence[str], suffixes: Sequence[str]) -> list[str]:
    return [
        rel
        for rel in repo.tracked
        if scope.under(rel, scans)
        and (not suffixes or rel.endswith(tuple(suffixes)))
        and not out_of_scope(rel)
        and not repo.excluded(rel)
    ]


def scan(repo: Repo, rule: Rule) -> list[str]:
    """Every hit outside the rule's owners and allowed files, every listed path that is gone, every stale allow."""
    problems = missing(repo, rule.label, [*rule.scans, *rule.owners, *rule.allow])
    rows = [[re.compile(p) for p in row] for row in rule.rows]
    unless = [re.compile(p) for p in rule.unless]
    owners = set(rule.owners)
    answered: set[str] = set()
    for rel in sorted(files_of(repo, rule.scans, rule.suffixes)):
        if rel in owners:
            continue
        try:
            lines = lines_of(rel, repo.read(rel), rule.code_only)
        # telemetry: surfaced — a file that does not tokenize is one of the rule's `problems`
        except SyntaxError as exc:
            problems.append(f"{rel}: does not tokenize, so {rule.label} cannot read it: {exc}")
            continue
        hits = [n for n, line in enumerate(lines, 1) if _hit(line, rows, unless)]
        if rel in rule.allow:
            if hits:
                answered.add(rel)
            continue
        problems += [f"{rel}:{n}: {rule.message} — {rule.remedy}" for n in hits]
    problems += [
        f"{rule.label}: stale exemption: {rel} answers no line; remove it from allow"
        for rel in sorted(set(rule.allow) - answered)
        if (repo.root / rel).exists()
    ]
    return problems
