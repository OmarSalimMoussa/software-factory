"""Every file the factory writes, as one index the Library, the reader and the search bar share: the goal, tasks,
requests, specs, checks and reflections, each with its id, title, status, time and searchable text."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from runtime.dashboard.views import taskwords
from runtime.dashboard.views.kit import INBOX_TONES, KIND_LOOK, REFLECT_TONES, Snap, epoch, first_line
from runtime.queue.formats import outcome_task

WANTS = ("settings", "queue", "goal", "reflections", "proposals", "sessions", "outage")
STATUS_TONES = {
    "building": "accent",
    "qa": "accent",
    "qa-clean": "accent",
    "designing": "accent",
    "turn": "accent",
    "merged": "accent",
    "queued": "info",
    "waiting": "info",
    "proposed": "warn",
    "review": "warn",
    "decide": "warn",
    "deprioritized": "warn",
    "approved": "good",
    "checked": "good",
    "on-staging": "good",
    "promoted": "good",
    "pass": "good",
    "fail": "bad",
    "task": "accent",
    "person-written": "violet",
    "draft": "warn",
}


@dataclass(frozen=True, slots=True)
class Doc:
    id: str
    kind: str
    title: str
    status: str
    inbox: str | None
    at: float
    text: str
    raw: dict[str, Any]

    def chips(self) -> list[dict[str, Any]]:
        out = [{"text": self.id, "tone": KIND_LOOK[self.kind][2], "mono": True}]
        tone = REFLECT_TONES.get(self.status) if self.kind == "reflection" else STATUS_TONES.get(self.status, "")
        out.append({"text": self.status, "tone": tone or ""})
        if self.inbox:
            out.append({"text": self.inbox, "tone": INBOX_TONES[self.inbox]})
        return out


def spec_id(task_id: str) -> str:
    return "s-" + task_id.removeprefix("t-")


def reflection_id(n: int) -> str:
    return f"sr{n}"


def request_status(outcome: str) -> str:
    if outcome_task(outcome):
        return "task"
    return outcome.split(" ", 1)[0] if outcome else "waiting"


def index(snap: Snap) -> list[Doc]:
    """Every document, newest first. A part of the state that cannot be read leaves its documents out of the index
    and raises, so the Library says what it could not read."""
    queue = snap.get("queue")
    docs: list[Doc] = []
    goal = snap.get("goal")
    docs.append(Doc("GOAL.md", "goal", "GOAL.md", "person-written", None, goal["changed"], goal["text"], goal))
    for t in queue["tasks"]:
        text = " ".join([t["title"], t["body"], t["cause"], *t["requests"]])
        docs.append(Doc(t["id"], "task", t["title"], t["status"], None, epoch(t["created"]), text, t))
    for r in queue["requests"]:
        status = request_status(r["outcome"])
        text = " ".join([r["body"], r["where"], r["outcome"]])
        docs.append(Doc(r["id"], "request", first_line(r["body"]), status, r["inbox"], epoch(r["at"]), text, r))
    for task_id, s in queue["specs"].items():
        docs.append(
            Doc(
                spec_id(task_id),
                "spec",
                s["title"],
                s["status"],
                None,
                epoch(s["updated"]),
                " ".join([s["title"], task_id, s["brief"]]),
                s,
            )
        )
    for c in queue["checks"]:
        status = "fail" if any(r["result"] == "fail" for r in c["rows"]) else "pass"
        text = " ".join([c["title"], c["commit"], c["notes"], *(r["scenario"] for r in c["rows"])])
        docs.append(Doc(c["id"], "check", c["title"], status, None, epoch(c["at"]), text, c))
    for n, e in enumerate(snap.get("reflections"), 1):
        title = e["text"] if len(e["text"]) <= 72 else e["text"][:70].rstrip() + "…"
        docs.append(
            Doc(
                reflection_id(n),
                "reflection",
                title,
                e["kind"],
                None,
                epoch(e["at"]),
                " ".join([e["text"], e["who"], e["where"]]),
                e,
            )
        )
    return sorted(docs, key=lambda d: d.at, reverse=True)


def match_rank(doc: Doc, query: str) -> int | None:
    """0 when the title or id holds the query, 1 when only the text does, None when nothing does."""
    q = query.lower()
    if q in doc.title.lower() or q in doc.id.lower():
        return 0
    return 1 if q in doc.text.lower() else None


def searched(docs: list[Doc], query: str) -> list[Doc]:
    ranked = [(rank, d) for d in docs if (rank := match_rank(d, query)) is not None]
    return [d for _, d in sorted(ranked, key=lambda x: (x[0], -x[1].at))]


def status_word(snap: Snap, doc: Doc) -> str:
    """A document's status as the search bar shows it: a task's own status word."""
    if doc.kind == "task":
        return taskwords.status(snap, doc.raw)["word"]
    return doc.status
