"""The search bar: one query over every file, agent, setting and page, found and ranked by the server."""

from __future__ import annotations

from typing import Any

from runtime.dashboard.views import agentwords, docs, settings
from runtime.dashboard.views.kit import KIND_LOOK, Parts, Query, Snap
from runtime.dashboard.views.shell import PAGES
from runtime.shared.models import RETIRED

WANTS = (*docs.WANTS, "cost")
PER_GROUP = 4  # the search bar's own layout: a few items per group, then "See all" in the Library
_DOC_GROUPS = (("Tasks", "task"), ("Requests", "request"), ("Specs", "spec"))
_LATE_GROUPS = (("Checks", "check"), ("Reflections", "reflection"), ("Goal", "goal"))


def _has(query: str, *texts: str) -> bool:
    q = query.lower()
    return any(q in t.lower() for t in texts)


def search(snap: Snap, query: str) -> dict[str, Any]:
    q = query.strip()
    if not q:
        return {"groups": [], "files": 0, "shown_files": 0}
    found = docs.searched(docs.index(snap), q)

    def doc_items(kind: str) -> list[dict[str, Any]]:
        return [
            {
                "kind": "doc",
                "id": d.id,
                "title": d.title,
                "sub": f"{d.id} · {docs.status_word(snap, d)}",
                "icon": {"kind": "ktile", "key": KIND_LOOK[d.kind].icon, "tone": KIND_LOOK[d.kind].tone},
            }
            for d in found
            if d.kind == kind
        ][:PER_GROUP]

    sessions = list(snap.get("sessions").values())
    agents = []
    for s in sessions:
        name, role, doing = agentwords.name(snap, s), agentwords.role(s), agentwords.doing(snap, s)
        if s["state"] != RETIRED and _has(q, name, role, doing):
            agents.append(
                {
                    "kind": "agent",
                    "id": s["id"],
                    "title": name,
                    "sub": f"{role} · {doing}",
                    "icon": {"kind": "ava", "key": s["family"], "text": agentwords.initials(s)},
                }
            )
    gone = []
    for s in sorted((s for s in sessions if s["state"] == RETIRED), key=lambda s: s["ended"], reverse=True):
        name, role = agentwords.name(snap, s), agentwords.role(s)
        if _has(q, name, role, s["task"]):
            gone.append(
                {
                    "kind": "hist",
                    "id": s["id"],
                    "title": name,
                    "sub": f"{role} · {agentwords.why(s)['text']}",
                    "icon": {"kind": "ava", "key": s["family"], "text": agentwords.initials(s)},
                }
            )
    rows = []
    for _, title, _, keys in settings.GROUPS:
        for key, label in keys:
            if _has(q, label, key, title):
                value = settings.shown(key, settings.saved(snap.settings, key))
                rows.append(
                    {
                        "kind": "setting",
                        "id": key,
                        "title": label,
                        "sub": f"{title} · {_plain(value)}",
                        "icon": {"kind": "icon", "key": "adjustments-horizontal"},
                    }
                )
    pages = [
        {"kind": "page", "id": pid, "title": label, "sub": "Page", "icon": {"kind": "icon", "key": icon}}
        for pid, label, icon, _ in PAGES
        if _has(q, label)
    ]
    groups = [("Pages", pages)] + [(label, doc_items(kind)) for label, kind in _DOC_GROUPS]
    groups += [("Agents", (agents[:PER_GROUP] + gone[: PER_GROUP - 1])), ("Settings", rows[:PER_GROUP])]
    groups += [(label, doc_items(kind)) for label, kind in _LATE_GROUPS]
    kept = [{"label": label, "items": items} for label, items in groups if items]
    shown_files = sum(1 for g in kept for i in g["items"] if i["kind"] == "doc")
    return {"groups": kept, "files": len(found), "shown_files": shown_files}


def _plain(value: Any) -> str:
    if isinstance(value, dict):
        return " · ".join(str(v) for v in value.values())
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def wants(query: Query) -> dict[str, Any]:
    return {"want": list(WANTS)}


def parts(snap: Snap, query: Query) -> Parts:
    return {"search": lambda: search(snap, query.get("q", ""))}
