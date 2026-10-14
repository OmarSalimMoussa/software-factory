"""Browser lanes: numbered copies of a browser tester's kind, so testers of one kind started together each get their
own browser.

Claude Code runs a single server for subagents whose server definitions match, and the first of them to finish closes
it for the rest. So lane n of a kind, from 2 up to caps.browsers_at_once, is a kind of its own named ``<kind>-<n>``,
whose browser starts under that name; lane 1 is the kind itself. Everywhere else a lane is the kind it copies.
"""

from __future__ import annotations

import re

from runtime.shared.settings import Settings

BROWSER = "qa-browser"  # the server a browser tester's launcher starts: `factory qa-browser <kind or lane>`
_LANE = re.compile(r"^(?P<kind>[A-Za-z][A-Za-z-]*?)-(?P<lane>[2-9]|[1-9][0-9]+)$")


def kind_of(name: str) -> str:
    """The kind a subagent type names: qa-regression-2 is a qa-regression, and qa-regression is itself."""
    found = _LANE.fullmatch(name)
    return found["kind"] if found else name


def names(settings: Settings, kind: str) -> list[str]:
    if kind not in settings.get_list("subagents.browser_personas"):
        return [kind]
    return [kind, *(f"{kind}-{n}" for n in range(2, settings.get_int("caps.browsers_at_once") + 1))]
