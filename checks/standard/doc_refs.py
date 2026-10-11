"""Doc refs: every relative link and backticked path in a living doc exists.

A tracked Markdown file describes the tree as it is now, so what it names must resolve: from the repo root, a
project root (a folder holding pyproject.toml or package.json, or its src/ or package), or any folder above the
doc. Resolution leans generous: a real path read as missing blocks good work, a miss only waits for the next run.
Escapes: ``<!-- doc-refs: external -->`` in a doc's first ten lines skips it whole (a doc about another repo);
``<!-- doc-refs: not-a-path `tok` -->`` exempts one token. Both fail when they protect nothing.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from os.path import normpath
from pathlib import Path
from typing import Any

from checks.runner import scope
from checks.runner.repo import Repo

ID = "doc-refs"
RULE = "Every path a living doc names exists"
VALUES_FILE = "doc-refs.toml"
# excluded: folders of docs that are not living (a corpus broken on purpose, dated records), each with its grounds.
VALUES = {"excluded": {str: str}}

EXTERNAL_MARKER = "<!-- doc-refs: external -->"
GENERATED_DIRS = frozenset({"node_modules", "dist", "target", "out"})
_FENCE_RE = re.compile(r"^(?:```|~~~).*?^(?:```|~~~)[ \t]*$", re.M | re.S)
_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_TICK_RE = re.compile(r"`([^`\n]+)`")
_ANCHOR_RE = re.compile(r":\d+(?:-\d+)?$")
_NOT_A_PATH_RE = re.compile(r"<!--\s*doc-refs:\s*not-a-path((?:\s*`[^`\n]+`)+)\s*-->")
_SKIP_CHARS = frozenset(' *?${}<>|"()[]')
_MIME = frozenset({"text", "application", "image", "audio", "video", "font", "multipart", "message"})


def project_bases(tracked: Iterable[str]) -> set[str]:
    tracked = list(tracked)
    bases, roots = {""}, set()
    for rel in tracked:
        parts = rel.split("/")
        if len(parts) > 1:
            bases.add(parts[0])
        if parts[-1] in ("pyproject.toml", "package.json"):
            roots.add("/".join(parts[:-1]))
    bases |= roots
    for rel in tracked:
        for root in roots:
            prefix = f"{root}/" if root else ""
            rest = rel[len(prefix) :].split("/") if rel.startswith(prefix) else []
            if len(rest) >= 2 and (rest[0] == "src" or rest[1] == "__init__.py"):
                bases.add(f"{prefix}{rest[0]}")
    return bases


def path_like(token: str) -> str | None:
    """The token as a repo path, or None when it is some other thing (a URL, a package, a placeholder)."""
    token = _ANCHOR_RE.sub("", token.strip().rstrip(".,;:")).split("#", 1)[0]
    if "/" not in token or token.startswith(("/", "~/", "@")) or ":/" in token:
        return None  # a bare file name names a kind of file; absolute and home paths, URLs, npm scopes are not ours
    if "..." in token or "…" in token or _SKIP_CHARS & set(token):
        return None
    segments = token.rstrip("/").split("/")
    if GENERATED_DIRS & set(segments) or segments[-1].endswith(".env"):
        return None  # generated output and env files are never tracked
    head, _, rest = token.partition("/")
    if "." in head and not head.startswith("."):
        return None  # a dotted first segment reads as a host name
    if head in _MIME and "/" not in rest:
        return None  # a media type
    if head.isdigit() or (len(head) == 1 and head.isupper()):
        return None  # a formula or a placeholder
    return token


def candidates(text: str) -> set[str]:
    body = _FENCE_RE.sub("", text)
    out: set[str] = set()
    for match in _LINK_RE.finditer(_TICK_RE.sub("", body)):
        target = match.group(1)
        if not target.startswith(("#", "mailto:")) and "://" not in target and target.split("#", 1)[0]:
            out.add(target.split("#", 1)[0])
    for match in _TICK_RE.finditer(body):
        found = path_like(match.group(1))
        if found is not None:
            out.add(found)
    return out


def _exists(path: Path, stem_match: bool) -> bool:
    if path.exists():
        return True
    # An extensionless reference (the TS/JS import convention) resolves when a file shares its stem.
    return stem_match and "." not in path.name and path.parent.is_dir() and any(path.parent.glob(f"{path.name}.*"))


def unresolved(root: Path, doc: str, found: set[str], bases: set[str]) -> set[str]:
    parts = doc.split("/")[:-1]
    here = bases | {"/".join(parts[:i]) for i in range(len(parts), 0, -1)}
    return {c for c in found if not any(_exists(Path(normpath(root / b / c)), not c.endswith("/")) for b in here)}


def _not_a_path(doc: str, text: str, scan: str, missing: set[str], found: set[str]) -> list[str]:
    appeared = set(_TICK_RE.findall(_FENCE_RE.sub("", scan)))
    out: list[str] = []
    for match in _NOT_A_PATH_RE.finditer(text):
        for token in _TICK_RE.findall(match.group(1)):
            if token not in appeared:
                out.append(f"{doc}: not-a-path `{token}` matches nothing: remove it")
            elif token not in found:
                out.append(f"{doc}: not-a-path `{token}` is never read as a path: remove it")
            elif token not in missing:
                out.append(f"{doc}: not-a-path `{token}` already resolves: remove it")
            missing.discard(token)
    return out


def run(repo: Repo, values: dict[str, Any]) -> list[str]:
    excluded: dict[str, str] = values["excluded"]
    docs = scope.living_docs(repo.tracked)
    problems = [
        f"{ID}: excluded {prefix} holds no doc: remove it from check-values/{VALUES_FILE}"
        for prefix in excluded
        if not any(scope.under(d, [prefix]) for d in docs)
    ]
    bases = project_bases(repo.tracked)
    for doc in scope.living_docs(repo.tracked, excluded):
        text = repo.read(doc)
        if EXTERNAL_MARKER in "\n".join(text.splitlines()[:10]):
            continue
        scan = _NOT_A_PATH_RE.sub("", text)
        found = candidates(scan)
        missing = unresolved(repo.root, doc, found, bases)
        problems += _not_a_path(doc, text, scan, missing, found)
        problems += [f"{doc}: `{miss}` does not resolve" for miss in sorted(missing)]
    return problems
