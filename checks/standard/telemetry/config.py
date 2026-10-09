"""The product's telemetry registry, built from check-values/error-telemetry.toml.

Components map path prefixes to the process a file runs in and the sink its errors reach; the longest matching prefix
wins. Processes and non-booting rows answer every entrypoint. Every row carries its grounds in the values file.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Any, Literal

from checks.runner import scope

Sink = Literal["sdk-logging", "js-log-module", "exit", "surface", "none"]
SINKS: tuple[str, ...] = ("sdk-logging", "js-log-module", "exit", "surface", "none")
Language = Literal["python", "ts", "shell", "dockerfile"]
WHERE = "check-values/error-telemetry.toml"

_SUFFIXES: dict[str, Language] = {".py": "python", ".ts": "ts", ".tsx": "ts", ".js": "ts", ".mjs": "ts", ".sh": "shell"}
_SHEBANG = re.compile(r"^#!.*?\b(?:(?P<python>python[\d.]*)|(?P<shell>bash|sh|dash))\b")
_BOOT = re.compile(r"^(?:(?P<receiver>[A-Za-z_$][\w$.]*)\.)?(?P<name>[A-Za-z_$][\w$]*)(?:\((?P<arg>[^()]*)\))?$")
_PREFIX = re.compile(r"^[A-Z][A-Z0-9_-]*:$")

COMPONENT = {
    "prefix": str,
    "name": str,
    "sink": str,
    "grounds": str,
    "reporters": [str],
    "seams": [str],
    "log_modules": [str],
}
SCHEMA = {
    "python_roots": [str],
    "first_party": [str],
    "excluded": {str: str},
    "test_support_dirs": [str],
    "scanned_testdata": [str],
    "vendored": [str],
    "vendored_prefix": str,
    "typescript_from": str,
    "quiet_calls": [str],
    "floating_entries": [str],
    "floating_components": [str],
    "vite_configs": [str],
    "components": [COMPONENT],
    "processes": {str: {"process": str, "boot": str}},
    "non_booting": {str: str},
    "sdk_owners": {str: [str]},
    "entry_owns_runtime": {str: str},
    "dist_to_src": {str: str},
}


@dataclasses.dataclass(frozen=True)
class Component:
    prefix: str
    name: str
    sink: Sink
    grounds: str
    reporters: frozenset[str] = frozenset()
    seams: frozenset[str] = frozenset()
    log_modules: frozenset[str] = frozenset()  # the log modules whose imported `log.warn`/`log.error` report


@dataclasses.dataclass(frozen=True)
class BootCall:
    """The call an entry file must make: ``receiver.name(first_arg, ...)``; an empty receiver is a bare call and a
    None ``first_arg`` matches any first argument."""

    name: str
    receiver: str = ""
    first_arg: str | None = None

    def shown(self) -> str:
        arg = f"'{self.first_arg}'" if self.first_arg is not None else ""
        return f"{self.receiver + '.' if self.receiver else ''}{self.name}({arg})"


def parse_boot(text: str) -> BootCall | None:
    match = _BOOT.match(text.strip())
    if match is None:
        return None
    arg = (match["arg"] or "").strip().strip("'\"")
    return BootCall(match["name"], match["receiver"] or "", arg or None)


@dataclasses.dataclass(frozen=True)
class Registry:
    components: tuple[Component, ...]
    python_roots: tuple[str, ...]
    first_party: frozenset[str]
    excluded: dict[str, str]
    test_support_dirs: frozenset[str]
    scanned_testdata: tuple[str, ...]
    vendored: tuple[str, ...]
    vendored_prefix: str
    typescript_from: str
    quiet_calls: frozenset[str]
    floating_entries: tuple[str, ...]
    floating_components: frozenset[str]
    vite_configs: frozenset[str]
    processes: dict[str, tuple[str, BootCall]]
    non_booting: dict[str, str]
    sdk_owners: dict[str, frozenset[str]]
    entry_owns_runtime: dict[str, str]
    dist_to_src: dict[str, str]

    def component_of(self, path: str) -> Component | None:
        matches = [
            row
            for row in self.components
            if path == row.prefix or (row.prefix.endswith("/") and path.startswith(row.prefix))
        ]
        return max(matches, key=lambda row: len(row.prefix)) if matches else None

    def is_vendored(self, path: str) -> bool:
        return path.startswith(self.vendored)

    def log_modules(self) -> frozenset[str]:
        return frozenset().union(*(row.log_modules for row in self.components))

    def language_of(self, path: str, first_line: str) -> Language | None:
        """The scanner ``path`` belongs to, or None when the check does not read it: tests, test support folders,
        ``testdata/`` corpora (but ``scanned_testdata``), vendored and dated trees, ``*.d.ts`` and excluded trees.
        A file with no suffix is read by its ``#!`` line."""
        parts = path.split("/")
        if path.startswith(tuple(self.excluded)) or self.test_support_dirs & set(parts[:-1]):
            return None
        if "testdata" in parts[:-1] and not path.startswith(self.scanned_testdata):
            return None
        name = parts[-1]
        if name.startswith("Dockerfile"):
            return "dockerfile" if scope.classify(f"{path}.sh") == "source" else None
        if "." not in name:
            shebang = _SHEBANG.match(first_line)
            if shebang is None:
                return None
            language: Language = "python" if shebang["python"] else "shell"
            suffix = ".py" if language == "python" else ".sh"
            return language if scope.classify(path + suffix) == "source" else None
        if scope.classify(path) != "source" or path.endswith(".d.ts"):
            return None
        return _SUFFIXES.get(path[path.rfind(".") :])


def _problems(values: dict[str, Any]) -> list[str]:
    out: list[str] = []
    prefixes: set[str] = set()
    for row in values["components"]:
        if row["sink"] not in SINKS:
            out.append(f"{WHERE}: component {row['name']!r} has sink {row['sink']!r}; use one of {', '.join(SINKS)}")
        if not row["prefix"] or not row["grounds"].strip():
            out.append(f"{WHERE}: component {row['name']!r} needs a prefix and its grounds")
        if row["prefix"] in prefixes:
            out.append(f"{WHERE}: two components claim {row['prefix']!r}; keep one")
        prefixes.add(row["prefix"])
    names = {row["name"] for row in values["components"]}
    out += [
        f"{WHERE}: floating_components names {name!r}, which no component is called"
        for name in values["floating_components"]
        if name not in names
    ]
    out += [
        f"{WHERE}: processes.{path}.boot {row['boot']!r} is not a call such as `boot()` or `Sdk.init`"
        for path, row in values["processes"].items()
        if parse_boot(row["boot"]) is None
    ]
    if not _PREFIX.match(values["vendored_prefix"]):
        out.append(f"{WHERE}: vendored_prefix must be an upper-case word and a colon, such as LOCAL:")
    for table in ("excluded", "non_booting", "entry_owns_runtime"):
        out += [f"{WHERE}: {table}.{key} needs its grounds" for key, why in values[table].items() if not why.strip()]
    return out


def build(values: dict[str, Any]) -> tuple[Registry | None, list[str]]:
    """The registry, or None and every problem that keeps the values from making one."""
    problems = _problems(values)
    if problems:
        return None, problems
    components = tuple(
        Component(
            row["prefix"],
            row["name"],
            row["sink"],
            row["grounds"],
            frozenset(row["reporters"]),
            frozenset(row["seams"]),
            frozenset(row["log_modules"]),
        )
        for row in values["components"]
    )
    processes: dict[str, tuple[str, BootCall]] = {}
    for path, row in values["processes"].items():
        boot = parse_boot(row["boot"])
        assert boot is not None
        processes[path] = (row["process"], boot)
    registry = Registry(
        components=components,
        python_roots=tuple(values["python_roots"]),
        first_party=frozenset(values["first_party"]),
        excluded=dict(values["excluded"]),
        test_support_dirs=frozenset(values["test_support_dirs"]),
        scanned_testdata=tuple(values["scanned_testdata"]),
        vendored=tuple(values["vendored"]),
        vendored_prefix=values["vendored_prefix"],
        typescript_from=values["typescript_from"],
        quiet_calls=frozenset(values["quiet_calls"]),
        floating_entries=tuple(values["floating_entries"]),
        floating_components=frozenset(values["floating_components"]),
        vite_configs=frozenset(values["vite_configs"]),
        processes=processes,
        non_booting=dict(values["non_booting"]),
        sdk_owners={family: frozenset(paths) for family, paths in values["sdk_owners"].items()},
        entry_owns_runtime=dict(values["entry_owns_runtime"]),
        dist_to_src=dict(values["dist_to_src"]),
    )
    return registry, []
