"""The afternoon's queue as the warden keeps it: the tasks with their logs and pictures, the users' requests, and
the check records.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from seed_kit import FIXTURES, ago, fixture, fixture_time, iso, write, write_picture

from runtime.queue import records
from runtime.shared.models import Request, Task

STATUS_OF = {
    "Building": "building",
    "Deprioritized, back first": "deprioritized",
    "Designing": "designing",
    "Needs your Go": "review",
    "Needs your decision": "decide",
    "Waiting for you": "proposed",
    "Queued": "queued",
    "On staging": "on-staging",
}


SURFACE_OF = {
    label: key
    for key, label in {
        "ui": "UI",
        "api": "API",
        "access": "Access",
        "desktop": "Desktop",
        "none": "No runtime change",
    }.items()
}


_DONE = [
    ("t-0016", "Billing page shows credit after a refund", 1, "G1", "major", ["API"]),
    ("t-0015", "Invoice list loads slowly with 200 items", 2, "G2", "minor", ["API"]),
    ("t-0013", "Invoice page: the Approve button jumps", 1, "G2", "minor", ["UI"]),
    ("t-0012", "Usage page: totals per day", 3, "G4", "minor", ["UI", "API"]),
]


def seed_tasks(repo: Path, now: float) -> tuple[list[Task], dict[str, list[dict[str, str]]]]:
    rows = [r for g in fixture("work-tasks.json")["tasks"]["groups"] for r in g["rows"] if not r.get("broken")]
    rows += [
        {"id": i, "title": title, "status": {"word": "On staging"}, "rung": rung, "goal": goal}
        | {"severity": {"text": severity}, "surfaces": surfaces}
        for i, title, rung, goal, severity, surfaces in _DONE
    ]
    rows += [
        {
            "id": p["id"],
            "title": p["title"],
            "status": {"word": "Parked"},
            "rung": None,
            "goal": "G5",
            "severity": {"text": "minor"},
            "surfaces": ["UI"],
            "why_parked": p["why"],
        }
        for p in fixture("work-parked.json")["parked"]["rows"]
    ]
    # A proposal's card, and the one waiting to be proposed next, lead with their goal line: here, the title.
    proposals = {
        s["task"]: s for s in [*fixture("home.json")["proposals"]["slots"], *fixture("home-reserve.json")["cards"]]
    }
    tasks, logs = [], {}
    for n, row in enumerate(rows):
        sheet = fixture(f"task/{row['id']}.json")["task"]
        status = STATUS_OF.get(row["status"]["word"], "qa" if row["status"]["word"].startswith("QA") else "parked")
        created = fixture_time(sheet["log"][0]["at"], now) if sheet["log"] else now - (10 - n % 10) * 86400 + n * 60
        cause = sheet["route"]["cause"] or ("app/views.py:12" if row["rung"] in (0, 1) else "")
        # A task that was ever proposed carries the why and the sketch it was proposed with.
        proposed = status not in ("queued", "parked")
        sources = [f"task-{row['id']}.svg", f"spec-s-{row['id'][2:]}.svg", *(["task-t-0027.svg"] if proposed else [])]
        found = next((name for name in sources if (FIXTURES / "pictures" / name).exists()), "")
        picture = write_picture(repo, row["id"], found) if found else ""
        rung = row["rung"]
        task = Task(
            id=row["id"],
            title=proposals[row["id"]]["line"] if row["id"] in proposals else row["title"],
            requests=[r["id"] for r in sheet["requests"]],
            goal_item=row["goal"] or "",
            severity=row["severity"]["text"],
            surfaces=[SURFACE_OF[s] for s in row["surfaces"]],
            status=status,
            created=iso(created),
            body=f"# {row['title']}\n\nWhat to change, and how to know it is fixed.",
            cause=cause,
            slices=0 if rung == 0 else 1,
            owner_class="large-visible" if rung == 4 else "none",
            score=float(sheet["kv"][2]["value"]) if sheet["kv"][2]["value"].isdigit() else None,
            run=None if status in ("queued", "parked") else rung,
            why=proposals[row["id"]]["why"]
            if row["id"] in proposals
            else ("The next step for its goal item." if proposed else ""),
            picture=picture,
            branch=sheet["branch"] or "",
        )
        tasks.append(task)
        to = {"proposed": "proposed", "parked": "parked"}.get(status, "queued")
        text = row.get("why_parked", "proposed by the orchestrator" if to == "proposed" else "made by triage")
        logs[task.id] = [
            {"at": iso(created), "actor": "orchestrator", "event": "triage", "text": "made by triage", "to": "queued"}
        ]
        logs[task.id] += [
            {"at": iso(fixture_time(e["at"], now)), "actor": "owner", "event": "note", "text": e["text"]}
            for e in sheet["log"][1:]
        ]
        if to != "queued":
            logs[task.id].append(
                {
                    "at": iso(created + 600),
                    "actor": "orchestrator",
                    "event": "propose" if to == "proposed" else "park",
                    "text": text,
                    "from": "queued",
                    "to": to,
                }
            )
        if status == "decide":  # a spent limit stopped its run while it was building
            logs[task.id].append(
                {
                    "at": iso(now - 3600),
                    "actor": "warden",
                    "event": "limits",
                    "text": "its rounds are used up",
                    "from": "building",
                    "to": "decide",
                }
            )
    return tasks, logs


def seed_requests(tasks: list[Task], now: float) -> list[Request]:
    """The requests the fixtures name, with real ids; each task's list is rewritten to those ids."""
    by_id: dict[str, dict[str, Any]] = {}
    for row in fixture("work-requests.json")["requests"]["rows"]:
        if row.get("broken"):
            continue
        by_id[row["id"]] = {
            "text": row["text"],
            "inbox": row["inbox"],
            "age": row["age"],
            "outcome": row["_outcome"],
            "severe": row["severe"],
        }
    owners: dict[str, str] = {}
    # A task the fixtures list no request for borrows one that triage set aside, so every task links one.
    spare = sorted(rid for rid, row in by_id.items() if row["outcome"] in ("parked", "noise"))
    for task in tasks:
        sheet = fixture(f"task/{task.id}.json")["task"]
        if not sheet["requests"]:
            task.requests = [spare.pop(0)]
            owners[task.requests[0]] = task.id
            continue
        task.requests = [r["id"] for r in sheet["requests"]]
        for r in sheet["requests"]:
            owners.setdefault(r["id"], task.id)
            by_id.setdefault(
                r["id"],
                {
                    "text": r["text"],
                    "inbox": "agents" if ":" in r["text"] else "users",
                    "age": r["age"],
                    "outcome": task.id,
                    "severe": False,
                },
            )
    out, real = [], {}
    for rid, row in by_id.items():
        at = now - ago(row["age"])
        day = datetime.fromtimestamp(at, UTC).strftime("%Y-%m-%d")
        real[rid] = f"r-{day}-{rid[2:]}"
        owner_task = owners.get(rid, "")
        outcome = {"waiting": "", "noise": "noise a duplicate", "parked": "parked not for this goal"}.get(
            row["outcome"], ""
        )
        if owner_task:
            outcome = "task " + owner_task
        inbox = row["inbox"]
        sender = {"users": "user-ada", "owner": "owner", "agents": "qa-visual"}[inbox]
        out.append(
            Request(
                id=real[rid],
                inbox=inbox,
                from_role=sender,
                at=iso(at),
                severity="severe" if row["severe"] and inbox != "users" else "normal",
                where="the app",
                body=row["text"],
                report="reports/x.md" if inbox == "users" else "",
                outcome=outcome,
            )
        )
    for task in tasks:
        task.requests = [real[r] for r in task.requests]
    return out


def seed_checks(repo: Path, now: float) -> None:
    for back, row in enumerate(fixture("work-checks.json")["checks"]["rows"]):
        rows = [
            records.ScenarioRow(r["scenario"], r["state"], 6 if r["state"] == "pass" else 3, "evidence/x")
            for r in row["results"]
        ]
        record = records.CheckRecord(
            row["id"],
            row["sha"],
            iso(now - (back + 1) * 3600),
            row["merge"],
            rows,
            "Every scenario of the default branch, walked.",
        )
        write(repo / "queue" / "checks" / f"{row['id']}.md", records.dump_check(record))
