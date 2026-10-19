"""Library: every file the factory writes, filtered by kind, time, status and inbox, and searched by the server."""

from __future__ import annotations

import html
import re
from datetime import UTC, date, datetime
from typing import Any

from runtime.dashboard.views import docs
from runtime.dashboard.views.kit import (
    KIND_LOOK,
    PartError,
    Parts,
    Query,
    Snap,
    day_label,
    iso,
    page,
    plural,
    when,
)

WANTS = docs.WANTS
RANGES = [["all", "Any time"], ["today", "Today"], ["d7", "7 days"], ["d30", "30 days"], ["custom", "Between"]]
INBOXES = [["all", "Any inbox"], ["agents", "Agents"], ["owner", "You"], ["users", "Users"], ["chat", "Chat"]]
_SNIPPET = 160


def marked(text: str, query: str) -> str:
    """``text`` escaped, with every match of ``query`` in <mark>."""
    if not query:
        return html.escape(text)
    pieces = re.split(f"({re.escape(query)})", text, flags=re.IGNORECASE)
    return "".join(f"<mark>{html.escape(p)}</mark>" if i % 2 else html.escape(p) for i, p in enumerate(pieces))


def snippet(text: str, query: str) -> str:
    flat = " ".join(text.split())
    at = flat.lower().find(query.lower()) if query else -1
    start = max(0, at - _SNIPPET // 3) if at >= 0 else 0
    piece = flat[start : start + _SNIPPET]
    return ("…" if start else "") + marked(piece, query) + ("…" if start + _SNIPPET < len(flat) else "")


def _bounds(snap: Snap, query: Query) -> tuple[float, float]:
    choice = query.get("range", "all")
    day = 86400.0
    today = snap.now - snap.now % day
    if choice == "all":
        return float("-inf"), float("inf")
    if choice == "today":
        return today, float("inf")
    if choice in ("d7", "d30"):
        return today - (int(choice[1:]) - 1) * day, float("inf")
    if choice == "custom":
        try:
            low = date.fromisoformat(query.get("from", ""))
            high = date.fromisoformat(query.get("to", ""))
        except ValueError as exc:
            raise PartError("Between needs two dates, from and to.") from exc
        return _midnight(low), _midnight(high) + day
    raise PartError(f"range must be one of {', '.join(r[0] for r in RANGES)}.")


def _midnight(day: date) -> float:
    return datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp()


def library(snap: Snap, query: Query) -> dict[str, Any]:
    every = docs.index(snap)
    q = query.get("q", "").strip()
    kinds_raw = query.get("kinds", "")
    kinds = set(kinds_raw.split(",")) if kinds_raw else set(KIND_LOOK)
    if kinds - set(KIND_LOOK):
        raise PartError(f"kinds are any of {', '.join(KIND_LOOK)}.")
    low, high = _bounds(snap, query)
    status = query.get("status", "all")
    inbox = query.get("inbox", "all")
    if inbox not in [i[0] for i in INBOXES]:
        raise PartError(f"inbox must be one of {', '.join(i[0] for i in INBOXES)}.")
    found = docs.searched(every, q) if q else every
    found = [d for d in found if low <= d.at < high and status in ("all", d.status) and inbox in ("all", d.inbox)]
    shown = [d for d in found if d.kind in kinds]
    chunk, more = page(shown, query, snap.settings.get_int("dashboard.page_size"))
    statuses = sorted({d.status for d in every})
    return {
        "kinds": [
            {
                "key": key,
                "label": look.group,
                "count": sum(1 for d in found if d.kind == key),
                "checked": key in kinds,
                "icon": look.icon,
                "tone": look.tone,
            }
            for key, look in KIND_LOOK.items()
        ],
        "ranges": RANGES,
        "range": query.get("range", "all"),
        "statuses": [["all", "Any status"], *([s, s] for s in statuses)],
        "status": status,
        "inboxes": INBOXES,
        "inbox": inbox,
        "query": q or None,
        "total": plural(len(shown), "file"),
        "results": [
            {
                "id": d.id,
                "kind": d.kind,
                "title_html": marked(d.title, q),
                "chips": d.chips(),
                "snippet_html": snippet(d.text, q),
                "date": day_label(iso(d.at)[:10]),
                "time": when(d.at)[-5:],
            }
            for d in chunk
        ],
        "more": more,
    }


def wants(query: Query) -> dict[str, Any]:
    return {"want": list(WANTS)}


def parts(snap: Snap, query: Query) -> Parts:
    return {"library": lambda: library(snap, query)}
