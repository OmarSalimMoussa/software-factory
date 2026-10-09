"""The facts every scanner produces and the rule engine reads.

A scanner never decides whether a site is handled: it records what is there (calls, raises, imports, marker
comments) and the rules judge. The TS scanner emits the same shapes as JSON; the bridge rebuilds them.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Literal, get_args

SiteKind = Literal[
    "except", "suppress", "catch", ".catch", ".then", "floating", "shell-swallow", "shell-hidden", "shell-strict"
]  # fmt: skip
SITE_KINDS: frozenset[str] = frozenset(get_args(SiteKind))
# A marker comment: `telemetry:` or, in a vendored file, an upper-case prefix such as `LOCAL:` before it.
MARKER_RE = re.compile(r"^([A-Z][A-Z0-9_-]*:\s*)?telemetry:")


@dataclasses.dataclass(frozen=True)
class CallRef:
    """One call. ``receiver`` is the dotted text before the name ("" for a bare call); ``first_arg`` is the first
    argument when it is a literal or a dotted name, else None. ``to_stderr`` marks ``print(..., file=sys.stderr)``."""

    name: str
    receiver: str = ""
    first_arg: str | None = None
    line: int = 0
    to_stderr: bool = False

    @property
    def dotted(self) -> str:
        return f"{self.receiver}.{self.name}" if self.receiver else self.name


@dataclasses.dataclass(frozen=True)
class CatchSite:
    """A place an error stops. ``line`` is the catch line; ``body_line`` the first line inside the handler, where a
    marker may sit. ``calls`` and ``raises`` cover the handler body without nested function bodies. ``handler`` is
    the named or call-built handler of ``.catch(name)``. ``scope_text`` is the enclosing function's source (the
    module's when there is none), which a ``surfaced`` marker's token must appear in."""

    path: str
    line: int
    kind: SiteKind
    detail: str
    body_line: int
    calls: tuple[CallRef, ...] = ()
    raises: bool = False
    handler: CallRef | None = None
    scope_text: str = ""


@dataclasses.dataclass(frozen=True)
class FunctionFacts:
    """A function or method; ``method`` is True for one defined directly in a class body."""

    path: str
    name: str
    method: bool
    calls: tuple[CallRef, ...]


@dataclasses.dataclass(frozen=True)
class ImportRef:
    """An import binding ``local`` in ``path``. ``target`` is the tracked file it resolves to, or None outside the
    repo; ``name`` is the imported symbol, or None when ``local`` names the whole module."""

    path: str
    local: str
    target: str | None
    name: str | None
    line: int
    type_only: bool = False
    specifier: str = ""


@dataclasses.dataclass(frozen=True)
class MarkerComment:
    """A comment whose text starts with a marker; ``text`` is the comment without its leader (``#``, ``//``)."""

    path: str
    line: int
    text: str


@dataclasses.dataclass(frozen=True)
class FileFacts:
    """Everything one scanner learned about one file. ``calls`` holds every call, deduplicated, for the boot check;
    ``warnings_warn`` the lines of ``warnings.warn(...)``; ``main_guard`` whether ``if __name__ == ...`` exists."""

    path: str
    sites: tuple[CatchSite, ...] = ()
    functions: tuple[FunctionFacts, ...] = ()
    imports: tuple[ImportRef, ...] = ()
    markers: tuple[MarkerComment, ...] = ()
    calls: tuple[CallRef, ...] = ()
    warnings_warn: tuple[int, ...] = ()
    main_guard: bool = False
