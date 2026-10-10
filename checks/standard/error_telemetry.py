"""Error telemetry: every caught error re-raises, reports, or says why; every entrypoint boots telemetry.

Scans tracked non-test Python, TS/JS, shell (``*.sh`` and suffixless ``#!`` scripts) and Dockerfile ``RUN`` lines. A
catch site that neither re-raises nor reports through its component's sink fails unless a valid
``telemetry: expected|surfaced|last-resort — <reason>`` marker says why. Registry coverage, the wrong-component and
boot checks and marker validity are zero tolerance. The product's components, entrypoints and SDK owners come from
check-values/error-telemetry.toml.
"""

from __future__ import annotations

from typing import Any

from checks.runner.repo import Repo
from checks.standard.telemetry import entrypoints, markers, ownership, python_scan, shell_scan, ts_bridge
from checks.standard.telemetry.config import SCHEMA, WHERE, Language, Registry, build
from checks.standard.telemetry.facts import FileFacts
from checks.standard.telemetry.rules import Engine, Violation

ID = "error-telemetry"
RULE = "Every catch site re-raises, reports, or carries a real telemetry reason; every entrypoint boots telemetry"
VALUES_FILE = "error-telemetry.toml"
VALUES = SCHEMA

_SINK_REMEDY = {
    "sdk-logging": "reports through stdlib logging: call logger.warning/error/exception{reporters}, re-raise",
    "js-log-module": "reports through its log module: call log.warn/log.error{reporters}, throw",
    "exit": "runs where stderr and the exit code are seen: log it, write it to stderr, exit non-zero{reporters}, "
    "re-raise",
    "surface": "has no SDK: show the error to the person{reporters}, throw",
    "none": "runs where no sink exists: throw",
}
_KIND_TEXT = {
    "silent": "stops the error and nothing reports it",
    "quiet-log": "only logs at info or debug, which never reaches the error tracker",
    "console-only": "only writes to the console, which is not the operator's screen",
    "suppress": "suppresses the error with no body to report from",
    "floating": "drops a promise where no global rejection handler runs; add a .catch",
    "shell-swallow": "swallows a failed command's exit status",
    "shell-hidden": "hides a command's stderr while nothing reads its exit status",
    "shell-strict": "runs without strict mode",
}


def _first_line(repo: Repo, rel: str) -> str:
    if "." in rel.rsplit("/", 1)[-1]:
        return ""
    with (repo.root / rel).open(encoding="utf-8", errors="replace") as handle:
        return handle.readline()


def collect(repo: Repo, registry: Registry) -> tuple[list[FileFacts], list[tuple[str, Language]], list[str]]:
    """Facts for every scanned, registered file; the scanned files with their languages; and the problems."""
    resolver = python_scan.Resolver(registry.python_roots, sorted(registry.first_party), set(repo.tracked))
    files: list[FileFacts] = []
    scanned: list[tuple[str, Language]] = []
    problems: list[str] = []
    ts_paths: list[str] = []
    for rel in repo.tracked:
        if repo.excluded(rel):
            continue
        language = registry.language_of(rel, _first_line(repo, rel))
        if language is None:
            continue
        if registry.component_of(rel) is None:
            problems.append(f"no component row for {rel} — add a components row to {WHERE} naming its process and sink")
            continue
        scanned.append((rel, language))
        if language == "ts":
            ts_paths.append(rel)
            continue
        text = repo.read(rel)
        if language == "python":
            try:
                files.append(python_scan.scan(rel, text, resolver))
            except SyntaxError as exc:
                problems.append(f"{rel} does not parse as Python: {exc.msg}")
        elif language == "shell":
            files.append(shell_scan.scan_script(rel, text))
        else:
            files.append(shell_scan.scan_dockerfile(rel, text))
    try:
        files += ts_bridge.scan(repo.root, ts_paths, repo.tracked, registry.typescript_from)
    except ts_bridge.ScanError as exc:
        problems.append(str(exc))
    return files, scanned, problems


def _marker_prefix(path: str, registry: Registry) -> str:
    leader = "//" if path.endswith((".ts", ".tsx", ".js", ".mjs")) else "#"
    return f"{leader} {registry.vendored_prefix + ' ' if registry.is_vendored(path) else ''}telemetry:"


def violation_message(violation: Violation, registry: Registry) -> str:
    site, comp = violation.site, violation.component
    reporters = sorted(comp.reporters | comp.seams)
    extra = f", call {'/'.join(reporters)}" if reporters else ""
    remedy = _SINK_REMEDY[comp.sink].format(reporters=extra)
    return (
        f"{site.path}:{site.line} — {violation.kind}: `{site.detail}` {_KIND_TEXT[violation.kind]}. Component "
        f"{comp.name} {remedy}, or mark it `{_marker_prefix(site.path, registry)} expected|surfaced|last-resort — "
        "<reason>`."
    )


def judge(engine: Engine, files: list[FileFacts], registry: Registry) -> tuple[list[Violation], list[str]]:
    """The unmarked violations and the marker problems."""
    found = {v.site: v for v in engine.violations()}
    pairs, leftovers = markers.attach([s for f in files for s in f.sites], [m for f in files for m in f.markers])
    problems = [f"marker at {c.path}:{c.line} — stale: it sits on no catch site; remove it" for c in leftovers]
    names = engine.helper_names() | frozenset().union(*(row.reporters | row.seams for row in registry.components))
    for site, comment in pairs.items():
        parsed = markers.parse(comment, registry)
        if isinstance(parsed, str):
            problems.append(parsed)
        elif site not in found:
            problems.append(
                f"marker at {comment.path}:{comment.line} — stale: the site already re-raises or reports; remove it"
            )
        else:
            problems += markers.reason_problems(parsed, site, names)
    unmarked = [v for site, v in found.items() if site not in pairs]
    return sorted(unmarked, key=lambda v: (v.site.path, v.site.line)), problems


def run(repo: Repo, values: dict[str, Any]) -> list[str]:
    registry, problems = build(values)
    if registry is None:
        return [f"{ID}: {p}" for p in problems]
    files, scanned, problems = collect(repo, registry)
    by_path = {f.path: f for f in files}
    resolver = python_scan.Resolver(registry.python_roots, sorted(registry.first_party), set(repo.tracked))
    entries, found = entrypoints.discover(repo.root, scanned, by_path, resolver, registry)
    problems += found
    problems += entrypoints.check(entries, by_path, registry)
    problems += ownership.sdk_problems(files, registry)
    problems += ownership.log_module_problems(files, registry)
    problems += ownership.warnings_problems(files, registry)
    unmarked, marker_problems = judge(Engine(files, registry), files, registry)
    problems += marker_problems
    problems += [violation_message(v, registry) for v in unmarked]
    return [f"{ID}: {p}" for p in problems]
