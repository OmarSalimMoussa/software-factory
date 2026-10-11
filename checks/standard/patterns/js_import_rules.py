"""JS import rules: browser code keeps the import rules its product states.

Each rule names the files it binds (a file, or a folder ending in /), the files they may not import, and the narrow
ways past it: an owner the rule does not bind, a type-only import (erased at build, so no runtime edge), or importing
only named values such as a pure type guard. Imports are read from the source with comments masked (``import`` and
``export ... from``, a bare ``import '...'`` and a dynamic ``import('...')``); a relative specifier resolves to a
tracked file, its extension and ``index`` inferred. An exemption carries grounds and must answer an import.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from checks.runner import scope
from checks.runner.mask import js_code
from checks.runner.repo import Repo
from checks.standard.patterns import engine

ID = "js-import-rules"
RULE = "Browser code keeps its import rules"
VALUES_FILE = "js-import-rules.toml"
VALUES = {
    "rules": [
        {
            "name": str,
            "importers": [str],
            "targets": [str],
            "owners": [str],
            "types_allowed": bool,
            "allowed_names": [str],
            "remedy": str,
        }
    ],
    "exempt": [{"rule": str, "importer": str, "grounds": str}],
}
_SUFFIXES = (".ts", ".tsx", ".js", ".mjs")
_STATEMENT_RE = re.compile(
    r"\b(?P<kw>import|export)\b(?P<type>\s+type\b)?(?P<clause>[^;'\"]*?)\bfrom\s*"
    r"(?P<q>['\"])(?P<spec>[^'\"]+)(?P=q)"
    r"|\bimport\s*(?P<q2>['\"])(?P<spec2>[^'\"]+)(?P=q2)"
    r"|\bimport\(\s*(?P<q3>['\"])(?P<spec3>[^'\"]+)(?P=q3)\s*\)"
)
_NAMED_RE = re.compile(r"\{([^}]*)\}")


@dataclass(frozen=True)
class Import:
    line: int
    target: str
    type_only: bool
    names: frozenset[str]  # value names taken; empty when unknowable


def _resolve(importer: str, spec: str, tracked: frozenset[str]) -> str:
    base = posixpath.normpath(posixpath.join(posixpath.dirname(importer), spec))
    for candidate in (base, *(base + s for s in _SUFFIXES), *(f"{base}/index{s}" for s in _SUFFIXES)):
        if candidate in tracked:
            return candidate
    return base


def imports(rel: str, text: str, tracked: frozenset[str]) -> Iterator[Import]:
    masked = "\n".join(js_code(text))
    position = 0
    while (match := _STATEMENT_RE.search(text, position)) is not None:
        # A keyword inside a comment or string is no import, but a real statement may start later inside this match.
        if masked[match.start()] != text[match.start()]:
            position = match.start() + 1
            continue
        position = match.end()
        spec = match.group("spec") or match.group("spec2") or match.group("spec3")
        if not spec.startswith("."):
            continue
        clause = match.group("clause") or ""
        named = _NAMED_RE.search(clause)
        parts = [p.strip() for p in named.group(1).split(",") if p.strip()] if named else []
        outside = _NAMED_RE.sub("", clause).replace(",", " ").split()
        taken = {p.split(" as ")[0].strip() for p in parts if not p.startswith("type ")}
        type_only = bool(match.group("type")) or (
            match.group("spec") is not None and bool(parts) and not taken and not outside
        )
        yield Import(
            line=text.count("\n", 0, match.start()) + 1,
            target=_resolve(rel, spec, tracked),
            type_only=type_only,
            names=frozenset(taken) if not outside else frozenset(),
        )


def _breaks(rule: dict[str, Any], rel: str, imp: Import) -> bool:
    if not scope.under(imp.target, rule["targets"]) or scope.under(rel, rule["owners"]):
        return False
    if rule["types_allowed"] and imp.type_only:
        return False
    allowed = frozenset(rule["allowed_names"])
    return not (allowed and imp.names and imp.names <= allowed)


def _listed_problems(repo: Repo, values: dict[str, Any]) -> list[str]:
    names = {rule["name"] for rule in values["rules"]}
    problems = [f"{ID}: exemption names no rule {row['rule']!r}" for row in values["exempt"]
                if row["rule"] not in names]  # fmt: skip
    for rule in values["rules"]:
        label = f"{ID} ({rule['name']})"
        problems += engine.missing(repo, label, [*rule["importers"], *rule["targets"], *rule["owners"]])
    return problems + engine.missing(repo, ID, [row["importer"] for row in values["exempt"]])


def run(repo: Repo, values: dict[str, Any]) -> list[str]:
    problems = _listed_problems(repo, values)
    exempt = {(row["rule"], row["importer"]) for row in values["exempt"]}
    tracked = frozenset(repo.tracked)
    answered: set[tuple[str, str]] = set()
    for rel in sorted(tracked):
        if not rel.endswith(_SUFFIXES) or engine.out_of_scope(rel):
            continue
        binding = [rule for rule in values["rules"] if scope.under(rel, rule["importers"])]
        if not binding:
            continue
        for imp in imports(rel, repo.read(rel), tracked):
            for rule in binding:
                if not _breaks(rule, rel, imp):
                    continue
                if (rule["name"], rel) in exempt:
                    answered.add((rule["name"], rel))
                    continue
                problems.append(f"{rel}:{imp.line}: imports {imp.target}, which breaks {rule['name']!r} — "
                                f"{rule['remedy']}")  # fmt: skip
    problems += [
        f"{ID}: stale exemption: {rel} does not break {name!r}; remove the row"
        for name, rel in sorted(exempt - answered)
        if (repo.root / rel).exists()
    ]
    return problems
