"""Wrong-component checks: code reports only through its own component.

The SDK is imported only by its owners; a TS file logs through its own package's log module; product Python never
calls ``warnings.warn``. Every check is zero tolerance, and an exemption no file needs is stale.
"""

from __future__ import annotations

from collections.abc import Iterable

from checks.standard.telemetry.config import WHERE, Registry
from checks.standard.telemetry.facts import FileFacts


def _sdk_family(specifier: str, registry: Registry) -> str | None:
    """The sdk_owners family ``specifier`` imports: a family ending in "/" is a package scope, any other is a
    module and its submodules."""
    for family in registry.sdk_owners:
        if family.endswith("/") and specifier.startswith(family):
            return family
        if specifier == family or specifier.startswith(f"{family}."):
            return family
    return None


def sdk_problems(files: Iterable[FileFacts], registry: Registry) -> list[str]:
    problems: list[str] = []
    importers: set[str] = set()
    for facts in files:
        for imp in facts.imports:
            family = _sdk_family(imp.specifier, registry)
            if family is None or imp.type_only:
                continue
            importers.add(facts.path)
            owners = registry.sdk_owners[family]
            if facts.path not in owners:
                problems.append(
                    f"{facts.path}:{imp.line} imports {imp.specifier} — only {', '.join(sorted(owners))} may; log "
                    "through the component's logger instead, so the error lands under the right process and tags"
                )
    owners_all = set().union(*registry.sdk_owners.values())
    problems += [
        f"stale sdk_owners entry {path}: it imports no SDK — remove it from {WHERE}"
        for path in sorted(owners_all - importers)
    ]
    return sorted(set(problems))


def log_module_problems(files: Iterable[FileFacts], registry: Registry) -> list[str]:
    log_modules = registry.log_modules()
    problems: list[str] = []
    crossing: set[str] = set()
    for facts in files:
        comp = registry.component_of(facts.path)
        for imp in facts.imports:
            if imp.type_only or imp.target not in log_modules or comp is None or imp.target in comp.log_modules:
                continue
            crossing.add(facts.path)
            if facts.path not in registry.entry_owns_runtime:
                problems.append(
                    f"{facts.path}:{imp.line} logs through {imp.target}, another package's log module — component "
                    f"{comp.name} logs through {', '.join(sorted(comp.log_modules)) or 'its own reporter seam'}"
                )
    problems += [
        f"stale entry_owns_runtime entry {path}: it imports no other package's log module — remove it from {WHERE}"
        for path in sorted(set(registry.entry_owns_runtime) - crossing)
    ]
    return problems


def warnings_problems(files: Iterable[FileFacts], registry: Registry) -> list[str]:
    problems: list[str] = []
    for facts in files:
        comp = registry.component_of(facts.path)
        if comp is None or comp.sink != "sdk-logging":
            continue
        problems += [
            f"{facts.path}:{line} — warnings.warn reaches no sink in product Python; call logger.warning"
            for line in facts.warnings_warn
        ]
    return problems
