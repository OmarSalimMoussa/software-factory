"""Every page the API answers: which state it asks the warden for, and the parts it builds from it."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from runtime.dashboard.views import (
    agent,
    agents,
    doc,
    guard,
    home,
    library,
    push,
    reflect,
    search,
    settings,
    shell,
    spend,
    task,
    work,
)
from runtime.dashboard.views.kit import Parts, Query, Snap


@dataclass(frozen=True, slots=True)
class Page:
    wants: Callable[[Query], dict[str, Any]]
    parts: Callable[[Snap, Query], Parts]


def _page(module: Any) -> Page:
    return Page(module.wants, module.parts)


_ID = r"([A-Za-z0-9][A-Za-z0-9._-]{0,127})"
PAGES: list[tuple[re.Pattern[str], Page, dict[str, str]]] = [
    (re.compile(r"^/api/shell$"), _page(shell), {}),
    (re.compile(r"^/api/home$"), _page(home), {}),
    (re.compile(r"^/api/work$"), _page(work), {}),
    (re.compile(rf"^/api/task/{_ID}$"), _page(task), {}),
    (re.compile(r"^/api/agents$"), _page(agents), {}),
    (re.compile(rf"^/api/agent/{_ID}$"), _page(agent), {"view": "agent"}),
    (re.compile(rf"^/api/agent/{_ID}/stop-preview$"), _page(agent), {"view": "preview"}),
    (re.compile(rf"^/api/history/{_ID}$"), _page(agent), {"view": "history"}),
    (re.compile(r"^/api/spend$"), _page(spend), {}),
    (re.compile(r"^/api/library$"), _page(library), {}),
    (re.compile(rf"^/api/doc/{_ID}$"), _page(doc), {}),
    (re.compile(r"^/api/settings$"), _page(settings), {}),
    (re.compile(r"^/api/reflect$"), _page(reflect), {}),
    (re.compile(r"^/api/guard$"), _page(guard), {}),
    (re.compile(r"^/api/search$"), _page(search), {}),
    (re.compile(r"^/api/push$"), _page(push), {}),
]
OP = re.compile(r"^/api/ops/(op-[0-9]{14}-[0-9a-f]{8})$")


def find(path: str, query: dict[str, str]) -> tuple[Page, dict[str, str]] | None:
    """The page at ``path``, with the ids in the path added to the query as ``id``."""
    for pattern, found, extra in PAGES:
        match = pattern.match(path)
        if match:
            return found, {**query, **extra, **({"id": match.group(1)} if match.groups() else {})}
    return None
