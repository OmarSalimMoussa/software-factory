"""Library filtering and the search bar, over the fixture index, in the shapes the dashboard contract gives."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fixture_state import JSON, mark, page_of

RANGE_DAYS = {"today": 1, "d7": 7, "d30": 30}
GROUPS = [
    ("Tasks", "task"),
    ("Requests", "request"),
    ("Specs", "spec"),
    ("Checks", "check"),
    ("Reflections", "reflection"),
    ("Goal", "goal"),
]
INITIALS = {"orch": "OR", "intake": "IN", "design": "DS", "impl": "IM", "qa": "QA"}


def stamp(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")


def matches(d: JSON, q: str) -> bool:
    return not q or q.lower() in f"{d['title']} {d['id']} {d['text']}".lower()


def snippet(d: JSON, q: str) -> str:
    text = d["text"]
    i = text.lower().find(q.lower()) if q else -1
    if i < 0:
        return mark(text[:140], q)
    return ("…" if i > 40 else "") + mark(text[max(0, i - 40) : i + 100], q)


def library(index: JSON, q: dict[str, str]) -> JSON:
    docs: list[JSON] = index["docs"]
    kinds = set(q["kinds"].split(",")) - {""} if "kinds" in q else {k["key"] for k in index["kinds"]}
    rng, status, inbox, text = (
        q.get("range", "all"),
        q.get("status", "all"),
        q.get("inbox", "all"),
        q.get("q", "").strip(),
    )
    now = stamp(index["now"])
    lo = (
        now - timedelta(days=RANGE_DAYS[rng])
        if rng in RANGE_DAYS
        else (stamp(q["from"] + "T00:00:00Z") if rng == "custom" and q.get("from") else None)
    )
    hi = stamp(q["to"] + "T23:59:59Z") if rng == "custom" and q.get("to") else None

    def keep(d: JSON) -> bool:
        t = stamp(d["at"])
        return (
            d["kind"] in kinds
            and (lo is None or t >= lo)
            and (hi is None or t <= hi)
            and status in ("all", d["status"])
            and inbox in ("all", d["inbox"])
            and matches(d, text)
        )

    found = [d for d in docs if keep(d)]
    shown, more = page_of(found, int(q.get("page", "1")))
    return {
        "kinds": [
            {**k, "count": sum(1 for d in docs if d["kind"] == k["key"]), "checked": k["key"] in kinds}
            for k in index["kinds"]
        ],
        "ranges": index["ranges"],
        "range": rng,
        "statuses": index["statuses"],
        "status": status,
        "inboxes": index["inboxes"],
        "inbox": inbox,
        "query": text or None,
        "total": len(found),
        "results": [
            {
                "id": d["id"],
                "kind": d["kind"],
                "title_html": mark(d["title"], text),
                "chips": d["chips"],
                "snippet_html": snippet(d, text),
                "date": d["date"],
                "time": d["time"],
            }
            for d in shown
        ],
        "more": more,
    }


def flat(n: JSON | None) -> list[JSON]:
    if n is None or n.get("broken"):
        return []
    return [n] + [x for k in n["kids"] for x in flat(k)]


def search(
    q: str,
    docs: list[JSON],
    pages: list[list[str]],
    tree: JSON | None,
    history: list[JSON],
    rows: list[JSON],
    groups: list[JSON],
) -> JSON:
    n = q.strip().lower()

    def has(s: str) -> bool:
        return n in s.lower()

    ranked = sorted([d for d in docs if matches(d, n)], key=lambda d: 0 if has(f"{d['title']} {d['id']}") else 1)
    out: list[JSON] = []
    page_items = [
        {"kind": "page", "id": p[0], "title": p[1], "sub": "Page", "icon": {"kind": "icon", "key": p[2]}}
        for p in pages
        if has(p[1])
    ]
    if page_items:
        out.append({"label": "Pages", "items": page_items})
    shown = 0
    agents = [
        {
            "kind": "agent",
            "id": a["id"],
            "title": a["name"],
            "sub": f"{a['role']} · {a['doing']}",
            "icon": {"kind": "ava", "key": a["family"], "text": a["initials"]},
        }
        for a in flat(tree)
        if has(f"{a['name']} {a['role']} {a['doing']}")
    ][:4]
    hist = [
        {
            "kind": "hist",
            "id": h["id"],
            "title": h["name"],
            "sub": f"{h['role']} · {h['why']['text']}",
            "icon": {"kind": "ava", "key": h["family"], "text": INITIALS[h["family"]]},
        }
        for h in history
        if not h.get("broken") and has(f"{h['name']} {h['role']} {h['task'] or ''}")
    ][:3]
    group_of = {r["key"]: g["title"] for g in groups for r in g["rows"]}
    sets: list[Any] = [
        {
            "kind": "setting",
            "id": r["key"],
            "title": r["label"],
            "sub": f"{group_of[r['key']]} · {r['saved'] if not isinstance(r['saved'], dict) else r['saved']['model']}",
            "icon": {"kind": "icon", "key": "adjustments-horizontal"},
        }
        for r in rows
        if not r.get("broken") and has(f"{r['label']} {r['key']} {group_of[r['key']]}")
    ][:4]
    for label, kind in GROUPS:
        items = [
            {
                "kind": "doc",
                "id": d["id"],
                "title": d["title"],
                "sub": f"{d['id']} · {d['status']}",
                "icon": {"kind": "ktile", "key": kind},
            }
            for d in ranked
            if d["kind"] == kind
        ][:4]
        shown += len(items)
        if items:
            out.append({"label": label, "items": items})
        if kind == "spec":
            if agents or hist:
                out.append({"label": "Agents", "items": agents + hist})
            if sets:
                out.append({"label": "Settings", "items": sets})
    return {"groups": out, "files": len(ranked), "shown_files": shown}
