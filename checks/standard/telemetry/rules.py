"""The handled verdict: which catch sites report, and which do not.

A reporting helper is a function whose body does what its component's sink accepts as reporting (a raise does not
count), directly or through another helper; the engine repeats the pass until no new helper appears. A site is handled
when it raises, reports, or calls a helper. Otherwise the verdict names the kind: ``silent``, ``quiet-log``,
``console-only``, ``suppress``, ``floating`` or a shell kind.
"""

from __future__ import annotations

import dataclasses
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence

from checks.standard.telemetry.config import Component, Registry
from checks.standard.telemetry.facts import CallRef, CatchSite, FileFacts, FunctionFacts

LOGGER_RE = re.compile(r"^_?(logger|log|LOG|LOGGER|logging)$")
_WARN_METHODS = frozenset({"warning", "warn", "error", "exception", "critical"})
_QUIET_METHODS = frozenset({"info", "debug"})
_WARN_LEVELS = frozenset({"WARNING", "WARN", "ERROR", "CRITICAL", "FATAL"})
_WARNING_LEVEL = 30  # logging.WARNING, for a level written as a number
_SELF = frozenset({"self", "cls", "this"})
_PY_EXIT = frozenset({"sys.exit", "exit"})
_TS_EXIT = frozenset({"process.exit", "process.exitCode=", "process.stderr.write", "console.error"})
_TS_REJECT = frozenset({"reject", "Promise.reject"})
_SHELL_KINDS = frozenset({"shell-swallow", "shell-hidden", "shell-strict"})
Action = Callable[[CallRef, str], bool]


@dataclasses.dataclass(frozen=True)
class Violation:
    site: CatchSite
    kind: str
    component: Component


def _is_python(path: str) -> bool:
    return path.endswith(".py") or "." not in path.rsplit("/", 1)[-1]


def _logger(receiver: str) -> bool:
    return bool(LOGGER_RE.match(receiver.rsplit(".", 1)[-1]))


def _level_at_least_warning(arg: str | None) -> bool:
    if arg is None:
        return False
    if arg.isdigit():
        return int(arg) >= _WARNING_LEVEL
    return arg.rsplit(".", 1)[-1] in _WARN_LEVELS


def _seam(call: CallRef, seams: frozenset[str]) -> bool:
    full = call.dotted
    return any(full == seam or full.endswith(f".{seam}") for seam in seams)


class Engine:
    def __init__(self, files: Sequence[FileFacts], registry: Registry):
        self.registry = registry
        self.files = {f.path: f for f in files}
        self.bindings = {f.path: {i.local: i for i in f.imports} for f in files}
        self.log_modules = registry.log_modules()
        self.functions: dict[tuple[str, str], list[FunctionFacts]] = defaultdict(list)
        for facts in files:
            for fn in facts.functions:
                key = f".{fn.name}" if fn.method else fn.name
                self.functions[(facts.path, key)].append(fn)
        self.reporting = self._fixpoint(self._reports)
        self.quiet = self._fixpoint(self._quiet) - self.reporting

    def helper_names(self) -> frozenset[str]:
        return frozenset(key[1].lstrip(".") for key in self.reporting)

    def _fixpoint(self, action: Action) -> set[tuple[str, str]]:
        found: set[tuple[str, str]] = set()
        changed = True
        while changed:
            changed = False
            for key, fns in self.functions.items():
                if key in found or self.registry.component_of(key[0]) is None:
                    continue
                if any(self._acts(c, fn.path, action, found) for fn in fns for c in fn.calls):
                    found.add(key)
                    changed = True
        return found

    def _acts(self, call: CallRef, path: str, action: Action, found: set[tuple[str, str]]) -> bool:
        if action(call, path):
            return True
        return any(key in found for key in self.resolve(call, path))

    def resolve(self, call: CallRef, path: str) -> Iterable[tuple[str, str]]:
        bound = self.bindings.get(path, {})
        if call.receiver in _SELF:
            return [(path, f".{call.name}")]
        if call.receiver:
            imp = bound.get(call.receiver)
            if imp is not None and imp.name is None and imp.target:
                return [(imp.target, call.name)]
            return []
        imp = bound.get(call.name)
        if imp is not None and imp.target and imp.name:
            return [(imp.target, imp.name)]
        return [(path, call.name)]

    def _log_import(self, receiver: str, path: str) -> bool:
        imp = self.bindings.get(path, {}).get(receiver)
        return imp is not None and imp.target in self.log_modules

    def _reports(self, call: CallRef, path: str) -> bool:
        """``call`` is an action ``path``'s sink accepts as reporting (a raise is not)."""
        comp = self.registry.component_of(path)
        if comp is None or comp.sink == "none":
            return False
        if call.name in comp.reporters or _seam(call, comp.seams):
            return True
        if comp.sink == "surface":
            return False
        if comp.sink == "exit" and self._exits(call, path):
            return True
        if _is_python(path):
            return _py_reports(call)
        return call.name in ("error", "warn") and self._log_import(call.receiver, path)

    def _exits(self, call: CallRef, path: str) -> bool:
        if _is_python(path):
            return call.dotted in _PY_EXIT or (call.name == "print" and call.to_stderr)
        return call.dotted in _TS_EXIT

    def _quiet(self, call: CallRef, path: str) -> bool:
        if _is_python(path):
            if call.receiver and _logger(call.receiver) and call.name == "log":
                return not _level_at_least_warning(call.first_arg)
            return bool(call.receiver) and _logger(call.receiver) and call.name in _QUIET_METHODS
        if call.receiver == "console" or (not call.receiver and call.name in self.registry.quiet_calls):
            return True
        return call.name in _QUIET_METHODS and (call.receiver == "log" or self._log_import(call.receiver, path))

    def _site_calls(self, site: CatchSite) -> tuple[CallRef, ...]:
        return site.calls + ((site.handler,) if site.handler else ())

    def handled(self, site: CatchSite) -> bool:
        if site.raises:
            return True
        for call in self._site_calls(site):
            if not _is_python(site.path) and call.dotted in _TS_REJECT:
                return True
            if self._acts(call, site.path, self._reports, self.reporting):
                return True
        return False

    def verdict(self, site: CatchSite, floating_scope: frozenset[str]) -> str | None:
        """The violation kind for ``site``, or None when it is handled."""
        if site.kind in _SHELL_KINDS or site.kind == "suppress":
            return site.kind
        if site.kind == "floating":
            return "floating" if site.path in floating_scope else None
        if self.handled(site):
            return None
        comp = self.registry.component_of(site.path)
        calls = self._site_calls(site)
        if comp is not None and comp.sink == "surface" and any(c.receiver == "console" for c in calls):
            return "console-only"
        if any(self._acts(c, site.path, self._quiet, self.quiet) for c in calls):
            return "quiet-log"
        return "silent"

    def floating_scope(self) -> frozenset[str]:
        """Files where a dropped promise meets no global rejection handler: the relative-import closure of each
        floating entry (type-only imports skipped), and every file of the floating components."""
        seen: set[str] = set()
        stack = [e for e in self.registry.floating_entries if e in self.files]
        while stack:
            path = stack.pop()
            if path in seen:
                continue
            seen.add(path)
            stack.extend(
                imp.target for imp in self.files[path].imports if imp.target in self.files and not imp.type_only
            )
        for path in self.files:
            comp = self.registry.component_of(path)
            if comp is not None and comp.name in self.registry.floating_components:
                seen.add(path)
        return frozenset(seen)

    def violations(self) -> list[Violation]:
        scope = self.floating_scope()
        found: list[Violation] = []
        for facts in self.files.values():
            comp = self.registry.component_of(facts.path)
            if comp is None:
                continue
            for site in facts.sites:
                kind = self.verdict(site, scope)
                if kind is not None:
                    found.append(Violation(site, kind, comp))
        return found


def _py_reports(call: CallRef) -> bool:
    if not call.receiver or not _logger(call.receiver):
        return False
    if call.name == "log":
        return _level_at_least_warning(call.first_arg)
    return call.name in _WARN_METHODS
