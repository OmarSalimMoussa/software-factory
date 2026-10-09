"""Self-reflect: what agents wrote about the factory itself, newest first, by kind. Read-only."""

from __future__ import annotations

from typing import Any

from runtime.dashboard.views.kit import REFLECT_TONES, PartError, Parts, Query, Snap, epoch, page, when

WANTS = ("settings", "reflections")


def reflect(snap: Snap, query: Query) -> dict[str, Any]:
    entries = list(reversed(snap.get("reflections")))
    kind = query.get("kind", "all")
    if kind != "all" and kind not in REFLECT_TONES:
        raise PartError(f"kind must be all or one of {', '.join(REFLECT_TONES)}.")
    kinds = [
        {"key": k, "count": sum(1 for e in entries if e["kind"] == k), "tone": tone}
        for k, tone in REFLECT_TONES.items()
    ]
    chosen = [e for e in entries if kind in ("all", e["kind"])]
    chunk, more = page(chosen, query, snap.settings.get_int("dashboard.page_size"))
    rows = [
        {
            "kind": e["kind"],
            "tone": REFLECT_TONES[e["kind"]],
            "who": e["who"],
            "where": e["where"],
            "when": when(epoch(e["at"])),
            "repeats": e["repeats"],
            "text": e["text"],
        }
        for e in chunk
    ]
    return {"kinds": kinds, "entries": rows, "more": more}


def wants(query: Query) -> dict[str, Any]:
    return {"want": list(WANTS)}


def parts(snap: Snap, query: Query) -> Parts:
    return {"reflect": lambda: reflect(snap, query)}
