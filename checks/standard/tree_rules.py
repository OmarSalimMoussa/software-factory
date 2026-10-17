"""Tree rules: empty ``__init__.py``, no barrels, a README per folder, no environment defaults, thin services.

1. An ``__init__.py`` holds nothing but comments: importers name the module that owns the symbol.
2. No barrel: no ``index.(ts|tsx|js|jsx|mjs)`` and no module that only re-exports, unless it is a listed entrypoint.
3. Every folder holding tracked files has a README.md, except check-values/ (values only) and .github/ (GitHub's
   own).
4. Product Python reads no environment default (``environ.get(k, d)``, ``getenv(k, d)``, ``... or d``,
   ``environ.setdefault(``): config loads once, every key required.
5. A service module (``service.py``, ``*_service.py``) imports no HTTP framework: routes parse, services decide.
Every exemption carries its grounds and fails as stale when it answers nothing.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

from checks.runner import mask, scope
from checks.runner.repo import Repo

ID = "tree-rules"
RULE = "Empty __init__.py, no barrels, a README per folder, no env defaults"
VALUES_FILE = "tree-rules.toml"
# product_python: folders whose Python is product code; service_frameworks: the HTTP frameworks a service never
# imports; entrypoints, readme_exempt and env_allow: path -> grounds.
VALUES = {
    "product_python": [str],
    "service_frameworks": [str],
    "entrypoints": {str: str},
    "readme_exempt": {str: str},
    "env_allow": {str: str},
}

# A README.md in .github/ would stand in for the repo's own on its GitHub page.
STANDARD_README_EXEMPT = ("check-values", ".github")
_WEB = (".ts", ".tsx", ".js", ".jsx", ".mjs")
_INDEX_RE = re.compile(r"(^|/)index\.(ts|tsx|js|jsx|mjs)$")
_RE_EXPORT_RE = re.compile(r"\bexport\s+(type\s+)?(\*|\{[^}]*\})[^;]*?\bfrom\b\s*[^;\n]*;?")
# Read over code tokens joined by single spaces (mask.python_code), so a docstring or comment naming a call is no call.
_ENV_DEFAULT_RE = re.compile(
    r"\benviron \. get \([^()]*,|\bgetenv \([^()]*,|\b(environ \. get|getenv) \([^()]*\) or\b"
    r"|\benviron \. setdefault \("
)
_SERVICE_RE = re.compile(r"(^|/)(\w+_)?service\.py$")


def _stale(allow: dict[str, str], answered: set[str], key: str) -> list[str]:
    return [
        f"stale exemption: remove {rel} from {key} in check-values/{VALUES_FILE}"
        for rel in sorted(set(allow) - answered)
    ]


def init_problems(repo: Repo) -> list[str]:
    out: list[str] = []
    for rel in repo.tracked:
        if PurePosixPath(rel).name != "__init__.py" or scope.vendored(rel):
            continue
        if any(line.strip() and not line.strip().startswith("#") for line in repo.read(rel).splitlines()):
            out.append(f"{rel}: carries code; __init__.py stays empty: import from the module that owns the symbol")
    return out


def barrel_problems(repo: Repo, allow: dict[str, str]) -> list[str]:
    out: list[str] = []
    answered: set[str] = set()
    for rel in repo.tracked:
        if not rel.endswith(_WEB) or scope.vendored(rel):
            continue
        named_index = bool(_INDEX_RE.search(rel))
        if rel in allow:
            if named_index:
                answered.add(rel)
            continue
        if named_index:
            out.append(f"{rel}: an index module is a barrel: importers name the module that owns the symbol")
            continue
        remainder, exports = _RE_EXPORT_RE.subn("", "\n".join(mask.js_code(repo.read(rel))))
        if exports and not remainder.strip():
            out.append(f"{rel}: only re-exports, a barrel: importers name the module that owns the symbol")
    return out + _stale(allow, answered, "entrypoints")


def readme_problems(repo: Repo, allow: dict[str, str]) -> list[str]:
    files = set(repo.tracked)
    folders = {str(p) for rel in files for p in PurePosixPath(rel).parents if str(p) != "."}
    out: list[str] = []
    answered: set[str] = set()
    for folder in sorted(folders):
        if scope.under(folder, STANDARD_README_EXEMPT):
            continue
        exempt = next((e for e in allow if scope.under(folder, [e])), None)
        if exempt is not None:
            answered.add(exempt)
        elif f"{folder}/README.md" not in files:
            out.append(f"{folder}: no README.md: every folder says what it is and how it fits")
    return out + _stale(allow, answered, "readme_exempt")


def _product_python(repo: Repo, trees: list[str]) -> list[str]:
    return [
        rel
        for rel in repo.tracked
        if rel.endswith(".py")
        and not scope.PY_TEST_RE.search(rel)
        and scope.under(rel, trees)
        and not scope.vendored(rel)
        and not repo.excluded(rel)
    ]


def env_problems(repo: Repo, trees: list[str], allow: dict[str, str]) -> list[str]:
    out: list[str] = []
    answered: set[str] = set()
    for rel in _product_python(repo, trees):
        try:
            code = mask.python_code(repo.read(rel))
        # telemetry: surfaced — a file that does not tokenize is a problem line in `out`, the check's verdict
        except SyntaxError as exc:
            out.append(f"{rel}: does not tokenize, so its environment reads cannot be checked: {exc}")
            continue
        hits = [n for n, line in sorted(code.items()) if _ENV_DEFAULT_RE.search(line)]
        if rel in allow:
            if hits:
                answered.add(rel)
            continue
        out += [
            f"{rel}:{n}: defaults an environment read, a silent fallback: load the key once, required, in the "
            "config module"
            for n in hits
        ]
    return out + _stale(allow, answered, "env_allow")


def service_problems(repo: Repo, trees: list[str], frameworks: list[str]) -> list[str]:
    if not frameworks:
        return []
    framework_re = re.compile(rf"^\s*(from|import)\s+({'|'.join(map(re.escape, frameworks))})\b")
    return [
        f"{rel}:{n}: a service imports the HTTP framework: routes parse input and map errors, the service "
        "raises typed errors"
        for rel in _product_python(repo, trees)
        if _SERVICE_RE.search(rel)
        for n, line in enumerate(repo.read(rel).splitlines(), 1)
        if framework_re.match(line)
    ]


def run(repo: Repo, values: dict[str, Any]) -> list[str]:
    trees = values["product_python"]
    missing = [
        f"{ID}: product_python names {tree}, which holds no tracked file"
        for tree in trees
        if not any(scope.under(rel, [tree]) for rel in repo.tracked)
    ]
    return (
        missing
        + init_problems(repo)
        + barrel_problems(repo, values["entrypoints"])
        + readme_problems(repo, values["readme_exempt"])
        + env_problems(repo, trees, values["env_allow"])
        + service_problems(repo, trees, values["service_frameworks"])
    )
