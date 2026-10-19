"""The Library's reader: one document's head, its front matter as a grid of toned values, and its text as safe HTML
whose ids open the documents they name."""

from __future__ import annotations

from typing import Any

from runtime.dashboard import markdown
from runtime.dashboard.views import docs, replay
from runtime.dashboard.views.kit import (
    INBOX_TONES,
    KIND_LOOK,
    REFLECT_TONES,
    SEVERITY_TONES,
    SURFACES,
    PartError,
    Parts,
    Query,
    Snap,
    ago,
    brief_url,
    plural,
    when,
)
from runtime.queue.formats import CHECK_ID, outcome

WANTS = (*docs.WANTS, "specs", "check_runs")


def _value(text: str, tone: str | None = None, doc: str | None = None) -> dict[str, Any]:
    return {"text": text, "tone": tone, "doc": doc}


def _fm(label: str, *values: dict[str, Any]) -> dict[str, Any]:
    return {"label": label, "values": list(values)}


def _status(status: str) -> dict[str, Any]:
    return _value(status, docs.STATUS_TONES.get(status, ""))


def _check_text(raw: dict[str, Any]) -> str:
    rows = "\n".join(
        f"| `{r['scenario']}` | {'**fail**' if r['result'] == 'fail' else r['result']} | {r['steps']} |"
        for r in raw["rows"]
    )
    table = f"| Scenario | Result | Steps run |\n| :--- | :---: | ---: |\n{rows}"
    return f"# {raw['title']}\n\n{raw['notes']}\n\n## Scenarios\n\n{table}"


def _proof(snap: Snap, check: str, runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Each run the check's testers walked: its screenshots, and its replay when its tester is known."""
    rows = []
    for n, run in enumerate(sorted(runs, key=lambda e: str(e["run"])), 1):
        names = (check, str(run["head"]), str(run["tester"]), str(run["run"]))
        note = replay.cleaned(snap, run["cleaned"]) if run["cleaned"] else None
        rows.append(
            {
                "name": names[2] if len(runs) == 1 else f"{names[2]} {n}",
                "note": note or (None if run["agent"] else "No replay: its tester is not known."),
                "shots": replay.shots(check, run),
                "replays": [{"label": "Replay", "url": replay.url(*names)}] if run["agent"] and not note else [],
            }
        )
    return {"rows": rows, "empty": "No screenshots were kept for this check."}


def reader(snap: Snap, ref: str) -> dict[str, Any]:
    every = docs.index(snap)
    doc = next((d for d in every if d.id == ref), None)
    if doc is None:
        raise PartError("This file is gone. It may have been removed.")
    known = {d.id for d in every}
    raw, brief, proof = doc.raw, None, None
    if doc.kind == "task":
        fm = [
            _fm("Id", _value(raw["id"])),
            _fm("Goal item", _value(raw["goal_item"] or "none")),
            _fm("Severity", _value(raw["severity"], SEVERITY_TONES[raw["severity"]])),
            _fm("Surfaces", _value(", ".join(SURFACES[s] for s in raw["surfaces"]))),
            _fm("Rung", _value("not planned" if raw["run"] is None else str(raw["run"]))),
            _fm("Status", _status(raw["status"])),
            _fm("Requests", *(_value(r, None, r if r in known else None) for r in raw["requests"])),
        ]
        text = raw["body"] if raw["body"].lstrip().startswith("#") else f"# {raw['title']}\n\n{raw['body']}"
        if raw["cause"]:
            text += f"\n\nCause: `{raw['cause']}`."
    elif doc.kind == "request":
        task = outcome(raw["outcome"]).task
        fm = [
            _fm("Id", _value(raw["id"])),
            _fm("Inbox", _value(raw["inbox"], INBOX_TONES[raw["inbox"]])),
            _fm("Severity", _value(raw["severity"], "bad" if raw["severity"] == "severe" else "")),
            _fm("Outcome", _value(raw["outcome"] or "waiting", "info", task if task in known else None)),
            _fm("Where", _value(raw["where"])),
        ]
        text = f"# {doc.title}\n\n{raw['body']}"
    elif doc.kind == "spec":
        task = ref.replace("s-", "t-", 1)
        body = snap.get("specs").get(task)
        if body is None:
            raise PartError(f"The spec of {task} is gone.")
        fm = [
            _fm("Id", _value(ref)),
            _fm("Task", _value(task, None, task if task in known else None)),
            _fm("Goal item", _value(raw["goal_item"] or "none")),
            _fm("Rung", _value("not planned" if raw["rung"] is None else str(raw["rung"]))),
            _fm("Status", _status(raw["status"])),
        ]
        text, brief = f"# {body['title']}\n\n{body['body']}", brief_url(raw["task"]) if raw["brief"] else None
    elif doc.kind == "check":
        fm = [
            _fm("Result", _status(doc.status)),
            _fm("Commit", _value(raw["commit"][:7])),
            _fm("When", _value(when(doc.at, utc=True))),
        ]
        text, proof = _check_text(raw), _proof(snap, raw["id"], snap.get("check_runs").get(raw["id"], []))
    elif doc.kind == "reflection":
        fm = [
            _fm("Kind", _value(raw["kind"], REFLECT_TONES[raw["kind"]])),
            _fm("Who", _value(raw["who"])),
            _fm("Where", _value(raw["where"] or "the factory")),
            _fm("When", _value(when(doc.at, utc=True))),
            _fm("Repeats", _value(plural(raw["repeats"], "time"))),
        ]
        text = raw["text"]
    else:
        fm, text = [_fm("Status", _value("person-written", "violet"))], raw["text"]
    return {
        "id": doc.id,
        "kind": doc.kind,
        "kind_label": KIND_LOOK[doc.kind].name,
        "kind_icon": KIND_LOOK[doc.kind].icon,
        "kind_tone": KIND_LOOK[doc.kind].tone,
        "title": doc.title,
        "when": f"{when(doc.at, utc=True)} · {ago(doc.at, snap.now)}",
        "fm": fm,
        "html": markdown.render(text, known, doc.id),
        "brief_url": brief,
        "proof": proof,
    }


def wants(query: Query) -> dict[str, Any]:
    ref = query.get("id", "")
    spec = [ref.replace("s-", "t-", 1)] if ref.startswith("s-") and ref[2:].isdigit() else []
    return {"want": list(WANTS), "specs": spec, "checks": [ref] if CHECK_ID.match(ref) else []}


def parts(snap: Snap, query: Query) -> Parts:
    return {"doc": lambda: reader(snap, query.get("id", ""))}
