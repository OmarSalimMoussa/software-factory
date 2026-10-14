"""A task's own panel: what it is, its route and progress, its testers and their evidence, what it was built from,
its requests, its log, its branch and, once it is done, its commits."""

from __future__ import annotations

from typing import Any

from runtime.dashboard.views import replay
from runtime.dashboard.views.kit import (
    PartError,
    Parts,
    Query,
    Snap,
    ago,
    brief_url,
    epoch,
    first_line,
    model_label,
    plural,
    version,
    when,
)
from runtime.dashboard.views.tasks import proposals
from runtime.dashboard.views.words import taskwords
from runtime.queue.formats import TASK_ID
from runtime.shared.models import OPEN_SEVERITIES, RETIRED

WANTS = ("settings", "queue", "proposals", "sessions", "cost", "outage", "cards", "logs", "qa", "specs", "done")
_IMAGE = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg")
UI_TESTERS = ("qa-ui-function", "qa-ux", "qa-visual")


def spec_id(task_id: str) -> str:
    return "s-" + task_id.removeprefix("t-")


def _latest_round(snap: Snap, task_id: str) -> tuple[str, list[str], dict[str, dict[str, Any]], list[str]]:
    """The head of the last ready record, its testers, each tester's last result on it, and its screenshots: the
    pictures at the top of each run, never its replay's frames."""
    qa = snap.get("qa").get(task_id, {"ready": [], "results": [], "evidence": []})
    if not qa["ready"]:
        return "", [], {}, []
    last = qa["ready"][-1]
    head = str(last["head"])
    results = {str(r["tester"]): r for r in qa["results"] if r["head"] == head}
    files = []
    for entry in qa["evidence"]:
        if entry["head"] == head:
            base = f"/api/evidence/{task_id}/{head[:12]}/{entry['tester']}/{entry['run']}"
            shots = [f for f in entry["files"] if "/" not in f and f.lower().endswith(_IMAGE)]
            files += [(entry["tester"], f"{base}/{f}") for f in shots]
    return head, list(last["testers"]), results, [f"{t}\t{u}" for t, u in files]


def _shots(files: list[str], tester: str) -> list[str]:
    return [line.split("\t", 1)[1] for line in files if line.split("\t", 1)[0] == tester]


def testers(snap: Snap, task: dict[str, Any]) -> dict[str, Any]:
    settings = snap.settings
    # The testers a round needs are the warden's, from the task and its diff, recorded when QA was readied.
    head, names, results, files = _latest_round(snap, task["id"])
    runs = replay.tester_runs(task["id"], head, snap.get("qa")[task["id"]]["evidence"]) if head else {}
    worker = taskwords.worker(snap, task["id"])
    live = {
        s["role"]
        for s in snap.get("sessions").values()
        if worker and s["parent"] == worker["id"] and s["state"] != RETIRED
    }
    rows = []
    for name in names:
        result = results.get(name)
        if result is None:
            state, tone, note = ("running", "accent", None) if name in live else ("waiting", "", None)
        elif any(result[f"{s}s"] for s in OPEN_SEVERITIES):
            state, tone = "fail", "bad"
            note = ", ".join(plural(result[f"{s}s"], s) for s in OPEN_SEVERITIES)
        else:
            state, tone, note = "pass", "good", None
        model = settings.model(name.replace("-", "_"))[0]
        rows.append(
            {
                "name": name,
                "model": model_label(model),
                "state": state,
                "tone": tone,
                "note": note,
                "shots": _shots(files, name),
                "replay": runs.get(name),
            }
        )
    if not head:
        return {"note": "decided once every slice is built", "rows": rows, "empty": "Testers start once every slice "
                "is built."}  # fmt: skip
    empty = "This change needs no testers: its own tests cover it."
    return {"note": f"{len(names)} required" if names else "none required", "rows": rows, "empty": empty}


def built_from(snap: Snap, task: dict[str, Any]) -> dict[str, Any] | None:
    """What a change on its way to prod was built from: a spec, or a fix with its cause and its QA."""
    if task["status"] not in taskwords.LANDED:
        return None
    spec = snap.get("specs").get(task["id"])
    if spec is not None:
        card = next((c for c in snap.get("cards") if c["task"] == task["id"] and c["kind"] == "spec"), None)
        go = task["id"] in snap.get("queue")["go"]
        picture = f"/api/picture/card/{card['id']}" if card and card["picture"] else None
        if picture is None and task["picture"]:
            picture = f"/api/picture/task/{task['id']}"
        return {
            "kind": "spec",
            "spec": spec_id(task["id"]),
            "summary": first_line(spec["body"], 300),
            "picture": picture,
            "brief_url": brief_url(task["id"]) if spec["brief"] else None,
            "approved_by": "you" if go else "the orchestrator (rung 3)",
        }
    n = taskwords.rung(task, snap.settings)
    _, names, results, files = _latest_round(snap, task["id"])
    qa = qa_line(names, results, files)
    body = task["body"].split("\n", 1)[1] if task["body"].startswith("#") else task["body"]
    return {
        "kind": "fix",
        "rung_label": f"rung {n} · no spec",
        "cause": task["cause"] or "not recorded",
        "fix": first_line(body, 300),
        "qa": qa,
    }


def qa_line(names: list[str], results: dict[str, dict[str, Any]], files: list[str]) -> dict[str, Any]:
    """What a fix's testers found on its last round: each UI tester's screenshots with their verdicts, or one line."""
    if not names:
        return {"tone": None, "note": "No testers: a small backend fix is covered by its own tests.", "shots": []}
    if set(names) & set(UI_TESTERS):
        shots = [_shot(t, u, _verdict(results.get(t))) for t in names for u in _shots(files, t)]
        return {"tone": None, "note": None, "shots": shots}
    said = ", ".join(f"{t} {_verdict(results.get(t))}" for t in names)
    return {"tone": "good", "note": said, "shots": []}


def _shot(tester: str, url: str, verdict: str) -> dict[str, Any]:
    return {"tester": tester, "url": url, "verdict": verdict, "tone": "good" if verdict == "pass" else "bad"}


def _verdict(result: dict[str, Any] | None) -> str:
    if result is None:
        return "no result"
    return "fail" if any(result[f"{s}s"] for s in OPEN_SEVERITIES) else "pass"


def sheet(snap: Snap, task_id: str) -> dict[str, Any]:
    task = taskwords.tasks(snap).get(task_id)
    if task is None:
        raise PartError(f"There is no task {task_id}.")
    settings = snap.settings
    n = taskwords.rung(task, settings)
    proposed = task["status"] == "proposed"
    asks = taskwords.requests_of(snap, task)
    chips = [{"text": task["title"], "tone": ""}] if proposed else []
    chips.append({"text": task["id"], "tone": "", "mono": True})
    chips.append(
        {"text": task["goal_item"], "tone": "accent"} if task["goal_item"] else {"text": "severe", "tone": "bad"}
    )
    chips += [{"text": s, "tone": ""} for s in taskwords.surfaces(task)]
    label, money = taskwords.money_or_estimate(snap, task)
    log = snap.get("logs").get(task_id, [])
    actions = ["library"]
    if proposed:
        actions += ["drop", "later", "approve"]
    elif task["status"] == "queued" and n != 0:
        actions.append("unboost" if task["boosted_at"] else "boost")
    return {
        "id": task["id"],
        "title": task["title"],
        "sheet_title": task["title"],
        "version": proposals.card_version(snap, task_id) if proposed else version(task),
        "proposal": {"picture": f"/api/picture/task/{task_id}" if task["picture"] else None, "why": task["why"]}
        if proposed
        else None,
        "status": taskwords.status(snap, task),
        "chips": chips,
        "kv": [
            {"label": "Requests", "value": str(len(task["requests"]))},
            {"label": "People", "value": str(len({r["from_role"] for r in asks}))},
            {"label": "Score", "value": "unknown" if task["score"] is None else str(round(task["score"]))},
            {"label": label, "value": money},
        ],
        "route": {
            "label": "The run you approve" if proposed else "Route",
            "steps": taskwords.route_steps(settings),
            "current": n,
            "cause": task["cause"] or None,
        },
        "progress": {"stages": taskwords.STAGES, "at": taskwords.progress_at(task, log)},
        "testers": testers(snap, task),
        "built_from": built_from(snap, task),
        "requests": [
            {"id": r["id"], "text": first_line(r["body"]), "age": ago(epoch(r["at"]), snap.now)}
            for r in sorted(asks, key=lambda r: r["at"], reverse=True)
        ],
        "log": [{"at": when(epoch(e["at"])), "text": e["text"]} for e in log],
        "branch": task["branch"] or None,
        "commits": _commits(snap, task_id),
        "actions": actions,
    }


def _commits(snap: Snap, task_id: str) -> list[dict[str, Any]]:
    """The commits of the merge that made the task done; none until Done lists it."""
    found = snap.get("done")
    row = next((t for t in found["tasks"] if t["task"] == task_id), None) if found is not None else None
    commits = row["commits"] if row is not None else []
    return [{"sha": c["sha"][:7], "subject": c["subject"], "note": f"slice {c['slice']}" if c["slice"] else None}
            for c in commits]  # fmt: skip


def _id(query: Query) -> str:
    task_id = query.get("id", "")
    if not TASK_ID.match(task_id):
        raise PartError(f"{task_id!r} is not a task id.")
    return task_id


def wants(query: Query) -> dict[str, Any]:
    task_id = _id(query)
    return {"want": list(WANTS), "tasks": [task_id], "specs": [task_id]}


def parts(snap: Snap, query: Query) -> Parts:
    return {"task": lambda: sheet(snap, _id(query))}
