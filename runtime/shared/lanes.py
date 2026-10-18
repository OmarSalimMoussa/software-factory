"""Browser lanes: numbered copies of a browser tester's kind, so testers of one kind started together each get their
own browser.

Claude Code runs a single server for subagents whose server definitions match, and the first of them to finish closes
it for the rest. So lane n of a kind, from 2 up to caps.browsers_at_once, is a kind of its own named ``<kind>-<n>``,
whose browser starts under that name; lane 1 is the kind itself. Everywhere else a lane is the kind it copies.

The machine has caps.browsers_at_once browser places. The guard refuses a browser tester past them
(`browsers-at-once`), and the warden tells a refused session once one frees: both count the places held here.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime
from typing import Protocol, TypeVar

from runtime.shared.models import WAITING, WORKING
from runtime.shared.settings import Settings

BROWSER = "qa-browser"  # the server a browser tester's launcher starts: `factory qa-browser <kind or lane>`
_LANE = re.compile(r"^(?P<kind>[A-Za-z][A-Za-z-]*?)-(?P<lane>[2-9]|[1-9][0-9]+)$")


def kind_of(name: str) -> str:
    """The kind a subagent type names: qa-regression-2 is a qa-regression, and qa-regression is itself."""
    found = _LANE.fullmatch(name)
    return found["kind"] if found else name


PLACES = "browsers-at-once"


class Row(Protocol):
    @property
    def id(self) -> str: ...
    @property
    def role(self) -> str: ...
    @property
    def state(self) -> str: ...
    @property
    def parent(self) -> str: ...
    @property
    def started(self) -> str: ...


R = TypeVar("R", bound=Row)


def tester_seconds(settings: Settings, items: int) -> int:
    """A tester's seconds: its start-up once, then each item's."""
    return settings.get_int("qa.setup_seconds") + items * settings.get_int("qa.seconds_per_item")


def holders(rows: Iterable[R], settings: Settings, now: datetime) -> list[R]:
    """The browser testers holding a place: working under a live session, and younger than any tester may last, since
    none outlasts its items' time, a desktop's start and a lost hand-back's quiet."""
    rows = list(rows)
    personas = settings.get_list("subagents.browser_personas")
    alive = {s.id for s in rows if s.state in (WORKING, WAITING)}
    waits = settings.get_int("slots.verb_minutes") * 60 + settings.get_int("qa.stale_seconds")
    longest = tester_seconds(settings, settings.get_int("qa.max_items")) + waits
    return [s for s in rows if s.parent in alive and s.state == WORKING and s.role in personas
            and (now - datetime.fromisoformat(s.started.replace("Z", "+00:00"))).total_seconds() < longest]  # fmt: skip


def names(settings: Settings, kind: str) -> list[str]:
    if kind not in settings.get_list("subagents.browser_personas"):
        return [kind]
    return [kind, *(f"{kind}-{n}" for n in range(2, settings.get_int("caps.browsers_at_once") + 1))]
