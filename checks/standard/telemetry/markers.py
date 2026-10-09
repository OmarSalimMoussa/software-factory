"""Telemetry markers: attaching them to sites and checking their reasons.

A marker sits trailing on the catch line, as the first line of the handler body, or on the line directly above, in
that order of preference. A valid marker clears its site's violation. A marker that is malformed, has a weak reason,
sits on a handled site or on no site is a problem, never a pass.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Sequence

from checks.standard.telemetry.config import Registry
from checks.standard.telemetry.facts import CatchSite, MarkerComment

KINDS = ("expected", "surfaced", "last-resort")
_GRAMMAR = re.compile(
    r"^(?P<prefix>[A-Z][A-Z0-9_-]*:)?\s*telemetry:\s*(?P<kind>[\w-]+)\s+(?:—|--)\s+(?P<reason>\S.*?)\s*$"
)
_GENERIC = frozenset(
    {
        "best effort",
        "ignore",
        "ignored",
        "intentional",
        "by design",
        "harmless",
        "fine",
        "safe to ignore",
        "not important",
        "expected",
        "swallow",
        "no-op",
    }
)
_REFUSED_ANYWHERE = re.compile(r"\b(todo|fixme|for now|temporarily|later)\b", re.I)
_TOKEN = re.compile(r"`([^`]+)`")
_MIN_WORDS = 4
_MIN_CHARS = 20


@dataclasses.dataclass(frozen=True)
class Marker:
    comment: MarkerComment
    kind: str
    reason: str


def parse(comment: MarkerComment, registry: Registry) -> Marker | str:
    """The marker in ``comment``, or the problem that makes it unreadable."""
    match = _GRAMMAR.match(comment.text)
    where = f"marker at {comment.path}:{comment.line}"
    if match is None:
        return f"{where} — malformed: write `telemetry: <kind> — <reason>` with kind one of {', '.join(KINDS)}"
    vendored = registry.is_vendored(comment.path)
    if vendored and match["prefix"] != registry.vendored_prefix:
        return f"{where} — a vendored file's marker takes the `{registry.vendored_prefix}` prefix"
    if match["prefix"] and not vendored:
        return f"{where} — the `{match['prefix']}` prefix belongs to vendored files only; drop it"
    if match["kind"] not in KINDS:
        return f"{where} — unknown kind {match['kind']!r}; use one of {', '.join(KINDS)}"
    return Marker(comment, match["kind"], match["reason"])


def reason_problems(marker: Marker, site: CatchSite, reporters: frozenset[str]) -> list[str]:
    """Every reason rule ``marker`` breaks for ``site``. ``reporters`` holds the registry's reporters and seams and
    the reporting helpers the engine found."""
    where = f"marker at {marker.comment.path}:{marker.comment.line}"
    reason = marker.reason
    normal = re.sub(r"[^\w\s-]", "", reason).strip().lower()
    problems: list[str] = []
    if normal in _GENERIC:
        problems.append(f'{where} — reason "{reason}" is generic; say why this site is safe')
    elif len(reason.split()) < _MIN_WORDS or len(reason) < _MIN_CHARS:
        problems.append(
            f'{where} — reason "{reason}" is too short (at least {_MIN_WORDS} words and {_MIN_CHARS} '
            "characters); say why this is not a failure"
        )
    refused = _REFUSED_ANYWHERE.search(reason)
    if refused:
        problems.append(
            f'{where} — reason says "{refused.group(0)}"; a marker records a decision, not a deferral — fix the '
            "site instead"
        )
    tokens = _TOKEN.findall(reason)
    # The marker sits inside the scope it names: without it removed, every token would find itself.
    scope = site.scope_text.replace(marker.comment.text, "")
    if marker.kind == "surfaced" and not any(t in scope for t in tokens):
        problems.append(
            f"{where} — `surfaced` must name, in backticks, where the error goes, and that name must appear in the "
            "enclosing function"
        )
    if marker.kind == "last-resort" and not any(t in reporters for t in tokens):
        problems.append(
            f"{where} — `last-resort` must name, in backticks, the reporter that failed (a registry reporter or a "
            "reporting helper)"
        )
    return problems


def attach(
    sites: Sequence[CatchSite], comments: Sequence[MarkerComment]
) -> tuple[dict[CatchSite, MarkerComment], list[MarkerComment]]:
    """Pairs each comment with at most one site of the same file, and each site with at most one comment; returns
    the pairs and the leftover comments."""
    by_line: dict[tuple[str, int], list[tuple[int, CatchSite]]] = {}
    for site in sites:
        for rank, line in enumerate((site.line, site.body_line, site.line - 1)):
            by_line.setdefault((site.path, line), []).append((rank, site))
    pairs: dict[CatchSite, MarkerComment] = {}
    leftovers: list[MarkerComment] = []
    for comment in sorted(comments, key=lambda c: (c.path, c.line)):
        options = sorted(by_line.get((comment.path, comment.line), []), key=lambda o: o[0])
        free = [site for _, site in options if site not in pairs]
        if free:
            pairs[free[0]] = comment
        else:
            leftovers.append(comment)
    return pairs, leftovers
