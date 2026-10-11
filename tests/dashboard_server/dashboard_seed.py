"""A warden state folder and a product checkout that hold what the UI's fixtures show, in the real formats.

The fixtures (testdata/dashboard-fixtures) are the UI's picture of one busy afternoon; this module writes the same
afternoon as the warden keeps it: registry, queue files, cards, proposals, costs, probes, deploys, plan limits,
vitals and reflections. The dashboard's views are then built from it, and must give back payloads of the fixtures'
shapes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runtime.queue import formats, records
from runtime.shared import claude_accounts, models, store, totp
from runtime.shared import settings as settings_module
from runtime.shared.models import CostRecord, Request, Session, Task
from runtime.shared.paths import Places
from runtime.warden import deploys, messages, public, settings_save

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "testdata" / "dashboard-fixtures"
T0 = 1_791_546_000.0  # 2026-10-09 11:40:00 UTC
TEST_TOKEN = "1"  # the device token the UI drives send as their owner cookie
MODELS = {"Opus 5.5": "claude-opus-5-5", "Sonnet 5.5": "claude-sonnet-5-5", "Haiku 5.5": "claude-haiku-5-5"}
PRICES = {"claude-opus-5-5": (15, 75), "claude-sonnet-5-5": (3, 15), "claude-haiku-5-5": (1, 5)}
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
STAGING, PROD, PREVIOUS = "a1b2c3d", "9f8e7d1", "5c4d3e2"  # a health answer may name a short commit
GOAL = """# The goal

## Who it is for
Small teams that send invoices every week.

## The goal
- G1 New teams sign up and send their first invoice
- G2 A first import runs end to end
- G3 Every screen fits a phone
- G4 Feedback reaches the queue the same day
- G5 Install from the app store
"""


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ago(text: str) -> float:
    """A fixture's "5 h ago" as seconds."""
    if text in ("just now", "0 min ago"):
        return 60.0
    if text == "yesterday":
        return 30 * 3600.0
    n = int(re.match(r"\d+", text).group(0)) if re.match(r"\d+", text) else 1  # type: ignore[union-attr]
    return n * {"min": 60, "h": 3600, "days": 86400}[text.split()[1]]


def _when(text: str, now: float) -> float:
    """A fixture's "Oct 9, 14:02" as a time on or before ``now``."""
    stamp = datetime.strptime(f"{datetime.fromtimestamp(now, UTC).year} {text}", "%Y %b %d, %H:%M")
    return min(stamp.replace(tzinfo=UTC).timestamp(), now - 60)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _picture(repo: Path, task_id: str, source: str) -> str:
    data = (FIXTURES / "pictures" / source).read_bytes()
    name = f"{task_id}-{hashlib.sha256(data).hexdigest()[:12]}.svg"
    _write(repo / "queue" / "pictures" / name, data.decode())
    return f"pictures/{name}"


def defaults_text(repo: Path) -> str:
    text = (ROOT / "settings.toml").read_text()
    for key, value in {
        "repo.path": str(repo),
        "repo.github": "acme/acme-app",
    }.items():
        text = settings_save.set_line(text, key, value)
    prices = "".join(
        f'"{m}" = {{ input = {i}, output = {o}, cache_read = 0, cache_write = 0 }}\n' for m, (i, o) in PRICES.items()
    )
    return text.replace("[prices]", "[prices]\n" + prices.rstrip("\n"), 1)


def _tasks(repo: Path, now: float) -> tuple[list[Task], dict[str, list[dict[str, str]]]]:
    rows = [r for g in fixture("work-tasks.json")["tasks"]["groups"] for r in g["rows"] if not r.get("broken")]
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
        created = _when(sheet["log"][0]["at"], now) if sheet["log"] else now - (10 - n % 10) * 86400 + n * 60
        cause = sheet["route"]["cause"] or ("app/views.py:12" if row["rung"] in (0, 1) else "")
        # A task that was ever proposed carries the why and the sketch it was proposed with.
        proposed = status not in ("queued", "parked")
        sources = [f"task-{row['id']}.svg", f"spec-s-{row['id'][2:]}.svg", *(["task-t-0027.svg"] if proposed else [])]
        found = next((name for name in sources if (FIXTURES / "pictures" / name).exists()), "")
        picture = _picture(repo, row["id"], found) if found else ""
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
            {"at": iso(_when(e["at"], now)), "actor": "owner", "event": "note", "text": e["text"]}
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
    return tasks, logs


def _requests(tasks: list[Task], now: float) -> list[Request]:
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
        at = now - _ago(row["age"])
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


COSTS: dict[str, float] = {}  # each agent's cost as the fixtures show it, filled as the agents are written


def _usd(text: str) -> float:
    return float(text.lstrip("$").replace(",", ""))


def _nodes(node: dict[str, Any]) -> list[dict[str, Any]]:
    return [node, *(n for kid in node.get("kids", []) for n in _nodes(kid))]


def _history_lines(sessions: list[Session]) -> str:
    """What the lifecycle writes: a line as each agent starts and as it retires; and one line someone broke."""
    lines = [{"at": s.started, "session": s.id, "name": s.name, "state": "working", "retired": "", "by": s.started_by,
              "why": "started"} for s in sessions]  # fmt: skip
    lines += [{"at": s.ended, "session": s.id, "name": s.name, "state": "retired", "retired": s.retired,
               "by": s.retired_by or "warden", "why": f"{s.retired} by the {s.retired_by or 'warden'}"}
              for s in sessions if s.state == "retired"]  # fmt: skip
    text = [json.dumps(line, sort_keys=True) for line in sorted(lines, key=lambda line: line["at"])]
    return "\n".join([*text[:5], '{"at": "2026-10-0', *text[5:]]) + "\n"


def _sessions(now: float) -> list[Session]:
    tree = fixture("agents.json")["tree"]
    tasks = {
        "impl-staging-down": "t-0021",
        "impl-sign-in-loop": "t-0017",
        "impl-retry-upload": "t-0022",
        "impl-project-names": "t-0020",
        "design-empty-states": "t-0014",
        "design-partner-sign-in": "t-0023",
        "qa-ui-1": "t-0017",
        "qa-vis-1": "t-0017",
        "qa-sec-1": "t-0017",
        "cr-ux": "t-0014",
    }
    slots = {"impl-sign-in-loop": 1, "impl-retry-upload": 2, "impl-staging-down": 3}
    roles = {"implementer, small": "implementer", "implementer, large": "implementer"}
    out = []

    def walk(node: dict[str, Any], parent: str, minutes: int) -> None:
        role = roles.get(node["role"], node["role"])
        if role in ("tester", "critic"):
            role = node["name"]
        state = {"working": "working", "waiting": "waiting", "depri": "deprioritized", "done": "retired"}[
            node["marker"]
        ]
        rung = {"implementer, small": 1, "implementer, large": 2}.get(node["role"])
        out.append(
            Session(
                id=node["id"],
                name=node["id"],
                role=role,
                family=node["family"],
                model=MODELS[node["model"]],
                effort="high",
                task=tasks.get(node["id"], ""),
                parent=parent,
                worktree=f"/srv/wt/{node['id']}",
                slot=slots.get(node["id"]),
                state=state,
                started=iso(now - minutes * 60),
                started_by="orchestrator",
                doing=node["doing"] if role.startswith(("qa-", "critic-", "intake")) else "",
                retired="finished" if state == "retired" else "",
                ended=iso(now - 60) if state == "retired" else "",
                note="benched for t-0021" if state == "deprioritized" else "",
                rung=rung,
                chat_link="https://claude.ai/code/session_0000example" if role == "orchestrator" else "",
                agent_id=f"agent-{node['id']}" if parent and role not in ("implementer", "designer") else "",
                since=iso(now - 600),
            )
        )
        for kid in node.get("kids", []):
            walk(kid, node["id"] if role not in ("orchestrator",) or kid["role"] in ("intake",) else "", minutes // 2)

    walk(tree, "", 312)
    COSTS.update({n["id"]: _usd(n["cost"]) for n in _nodes(tree)})
    for n in range(1, 33):
        h = fixture(f"history/h{n}.json")["history"]
        COSTS[h["id"]] = _usd(h["cost"])
        started, ended = _when(h["started"], now - 3600), _when(h["ended"], now - 1800)
        role = h["name"] if h["role"] in ("tester", "critic") else roles.get(h["role"], h["role"])
        kind = {"finished": "finished", "crashed": "crashed"}.get(h["why"]["text"], "stopped")
        out.append(
            Session(
                id=h["id"],
                name=h["id"],
                role=role,
                family=h["family"],
                model=MODELS[h["model"]],
                effort="high",
                task=h["task"] or "",
                parent="",
                worktree=f"/srv/wt/{h['id']}",
                slot=None,
                state="retired",
                started=iso(min(started, ended - 60)),
                started_by="orchestrator",
                retired=kind,
                ended=iso(ended),
                retired_by="owner" if "you" in h["why"]["text"] else "warden",
                note=h["note"],
                since=iso(ended),
            )
        )
    return out


def _cost(sessions: list[Session], tasks: list[Task], now: float) -> list[CostRecord]:
    out = []
    chart = fixture("spend-range-14.json")["chart"]
    families = [f["key"] for f in chart["families"]]
    for back, day in enumerate(reversed(chart["days"][:-1]), 1):
        for family, usd in zip(families, day["values"], strict=True):
            out.append(
                CostRecord(
                    at=iso(now - back * 86400),
                    session="orchestrator",
                    subagent="",
                    role="orchestrator",
                    family=family,
                    task="",
                    model="claude-sonnet-5-5",
                    input=1000,
                    output=100,
                    cache_read=0,
                    cache_write=0,
                    usd=float(usd),
                )
            )
    for s in sessions:
        if s.id in COSTS:
            out.append(
                CostRecord(
                    at=s.ended or iso(now - 120),
                    session=s.parent or s.id,
                    subagent=s.agent_id,
                    role=s.role,
                    family=s.family,
                    task=s.task,
                    model=s.model,
                    input=2000,
                    output=300,
                    cache_read=0,
                    cache_write=0,
                    usd=COSTS[s.id],
                )
            )
    out += [
        CostRecord(
            at=iso(now - 300),
            session="orchestrator",
            subagent="",
            role="orchestrator",
            family="orch",
            task="",
            model="claude-unpriced-1-0",
            input=10,
            output=1,
            cache_read=0,
            cache_write=0,
            usd=None,
        )
    ] * 2
    return out


def _checks(repo: Path, now: float) -> None:
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
        _write(repo / "queue" / "checks" / f"{row['id']}.md", records.dump_check(record))


def seed(root: Path, now: float = T0) -> Places:
    """Write the afternoon under ``root``; the warden's Places for it."""
    places = Places.under(root)
    repo = root / "repo"
    (repo / "queue").mkdir(parents=True, exist_ok=True)
    places.install.mkdir(parents=True, exist_ok=True)
    places.defaults.write_text(defaults_text(repo))
    places.state.mkdir(parents=True, exist_ok=True)
    places.totp_secret.write_text(totp.new_secret())
    # Rows per page as the UI's drives page them, the implementers' cap raised from its default, and an estimate as
    # soon as a rung has one landed run.
    store.write_text(
        places.overrides,
        "[caps]\nimplementers = 4\n\n[dashboard]\npage_size = 25\n\n[routing]\n" "estimate_min_runs = 1\n",
    )
    settings = settings_module.load(places.defaults, places.overrides)
    public.write_settings(places, settings)
    (repo / "GOAL.md").write_text(GOAL)
    tasks, logs = _tasks(repo, now)
    requests = _requests(tasks, now)
    for task in tasks:
        _write(repo / "queue" / "tasks" / f"{task.id}.md", formats.dump_task(task))
        _write(
            repo / "queue" / "tasks" / f"{task.id}.log.jsonl",
            "".join(records.dump_log(e).decode() for e in logs[task.id]),
        )
    for req in requests:
        _write(repo / "queue" / "requests" / req.inbox / f"{req.id}.md", formats.dump_request(req))
    for task_id, status, title in (
        ("t-0012", "approved", "Usage page: totals per day"),
        ("t-0014", "draft", "Clearer empty states"),
        ("t-0023", "review", "Sign-in from the build step"),
    ):
        spec = records.Spec(
            task_id,
            "G2",
            3,
            status,
            "https://claude.ai/code/artifact/00000000-0000-4000-8000-0000000" "00012",
            iso(now - 86400),
            title,
            "Usage shows each day's spend as a bar.",
        )
        _write(repo / "queue" / "specs" / f"{task_id}.md", records.dump_spec(spec))
    _checks(repo, now)
    _write(repo / "queue" / "tasks" / "t-0026.md", "---\nid: t-0026\n---\n# A task file someone broke\n")
    _write(repo / "queue" / "requests" / "users" / "r-2026-10-07-0032.md", "not a request\n")
    sessions = _sessions(now)
    store.write_text(places.history, _history_lines(sessions))
    store.write_json(places.registry, {s.id: models.dump(s) for s in sessions})
    store.write_json(places.state / "doing" / "impl-sign-in-loop.json", {"qa_round": 2})
    for n, holder in ((1, "impl-sign-in-loop"), (2, "impl-retry-upload"), (3, "impl-staging-down")):
        store.write_text(
            places.slot(n) / "lease",
            f"checkout=/srv/wt/{holder}\nholder={holder}\nsince={iso(now)}\n" "note=building\n",
        )
    store.write_json(places.proposals, {"open": ["t-0027", "t-0024", "t-0025"], "approved": []})
    cards = []
    for n, (kind, task_id, ask) in enumerate(
        (
            ("spec", "t-0023", "Sign off the partner sign-in spec"),
            ("decide", "t-0019", "Its rounds are used up at rung 3."),
            ("question", "t-0022", "Resume from the last part, or start again?"),
        ),
        1,
    ):
        picture = ""
        if kind != "question":
            data = (FIXTURES / "pictures" / f"card-n{n}.svg").read_bytes()
            picture = f"n{n}-{hashlib.sha256(data).hexdigest()[:12]}.svg"
            places.pictures.mkdir(parents=True, exist_ok=True)
            (places.pictures / picture).write_bytes(data)
        cards.append(
            models.Card(
                id=f"n{n}",
                kind=kind,
                task=task_id,
                title=next(t.title for t in tasks if t.id == task_id),
                ask=ask,
                created=iso(now - 3600),
                picture=picture,
                brief="https://claude.ai/code/artifact/x" if kind == "spec" else "",
                comments=3 if kind == "spec" else 0,
                asked_by="impl-retry-upload",
                comments_at=iso(now - 7200) if kind == "spec" else "",
            )
        )
    store.write_json(places.cards, [models.dump(c) for c in cards])
    for record in _cost(sessions, tasks, now):
        store.append_jsonl(places.cost, models.dump(record))
    for minute in range(200, 0, -1):
        for env, commit in (("staging", STAGING), ("prod", PROD)):
            ok = not (env == "staging" and minute == 193)
            probe = models.Probe(
                iso(now - minute * 60), env, ok, commit if ok else "", 80, "" if ok else "HTTP 502", counts=not ok
            )
            store.append_jsonl(places.probes, models.dump(probe))
    for when, env, commit in (("Oct 5, 09:12", "prod", PREVIOUS), ("Oct 7, 10:05", "prod", PROD),
                              ("Oct 9, 11:18", "staging", STAGING)):  # fmt: skip
        deploys.record(places, env, commit, iso(_when(when, now)))
    store.write_json(
        places.plan,
        {
            "written_at": now - 180,
            "copied_at": now - 170,
            "five_hour": {"used_percentage": 62, "resets_at": now + 8040},
            "seven_day": {"used_percentage": 41, "resets_at": now + 328000},
        },
    )
    readings = [
        ("memory", "green", "11.8 GiB free"),
        ("cpu", "green", "pressure 34% over a minute"),
        ("power", "green", "on mains power"),
        ("install", "green", "install 0123456789ab"),
        ("hooks", "green", "the checkout runs the factory's hooks"),
        ("daemon", "green", "inside the slice"),
    ]
    store.write_json(
        places.vitals,
        {"at": iso(now), "level": "green", "readings": [{"name": n, "level": lv, "value": v} for n, lv, v in readings]},
    )
    store.write_json(
        places.state / "docker-disk.json", {"at": iso(now), "bytes": 38 * 1024**3, "budget_bytes": 60 * 1024**3}
    )
    store.write_json(
        places.state / "triage-last.json",
        {
            "at": iso(now - 7200),
            "batch": 100,
            "new": ["t-0027", "t-0028", "t-0029", "t-0030"],
            "joined": 31,
            "noise": 52,
            "parked": 13,
        },
    )
    entries = []
    for e in fixture("reflect.json")["reflect"]["entries"]:
        at = iso(_when(e["when"], now))
        entries.append(
            {
                "at": at,
                "kind": e["kind"],
                "who": e["who"],
                "where": e["where"],
                "text": e["text"],
                "repeats": e["repeats"],
                "last": at,
            }
        )
    store.write_json(places.state / "reflections.json", sorted(entries, key=lambda e: e["at"]))
    for rule, count in (("tester-time", 9), ("subagents-at-once", 6), ("git-bypass", 3)):
        for _ in range(count):
            store.append_jsonl(
                places.refusals, {"at": iso(now - 600), "session": "x", "rule": rule, "tool": "Bash", "detail": ""}
            )
    store.append_jsonl(
        places.audit,
        {
            "at": iso(now - 86400),
            "by": "owner",
            "commit": "4be21c0" + "0" * 33,
            "changes": [{"key": "caps.implementers", "was": 3, "now": 3}],
        },
    )
    _evidence(places, now)
    _transcript(places, now)
    # The queue is the warden's own and writable by no one else, or it refuses to read it.
    for folder in [repo, *(d for d in repo.rglob("*") if d.is_dir())]:
        os.chmod(folder, 0o755)
    devices = {hashlib.sha256(TEST_TOKEN.encode()).hexdigest(): now + 365 * 86400}
    store.write_json(places.state / "devices.json", devices)
    secrets = places.state / "secrets"
    for name, days in (("main", 9), ("work", 2)):
        path = claude_accounts.token_file(secrets, name)
        store.write_text(path, f"sk-ant-oat01-seeded-token-{name}\n", mode=0o600)
        os.utime(path, (now - days * 86400, now - days * 86400))
    store.write_text(secrets / claude_accounts.UNIT_TOKEN, "sk-ant-oat01-seeded-token-main\n", mode=0o600)
    store.write_text(claude_accounts.folder(secrets) / claude_accounts.ACTIVE, "main\n", mode=0o600)
    return places


def _evidence(places: Places, now: float) -> None:
    """QA on two tasks: the sign-in loop's round in progress, and the Approve button's finished round."""
    done = next(sid for sid, row in store.read_json(places.registry).items() if row["task"] == "t-0013")
    rounds = (
        (
            "impl-sign-in-loop",
            "t-0017",
            "e" * 40,
            ["qa-ui-function", "qa-ux", "qa-visual", "qa-security"],
            {"qa-ux": 0, "qa-visual": 1, "qa-security": 0},
            {"qa-ui-function": "page", "qa-visual": "phone"},
        ),
        (
            done,
            "t-0013",
            "d" * 40,
            ["qa-ui-function", "qa-ux", "qa-visual"],
            {"qa-ui-function": 0, "qa-ux": 0, "qa-visual": 0},
            {"qa-ui-function": "page", "qa-visual": "button", "qa-ux": "phone"},
        ),
    )
    for session, task, head, testers, results, shots in rounds:
        ready = {"records": [{"head": head, "slices": [head], "at": iso(now - 900), "slot": 1, "testers": testers}]}
        store.write_json(places.state / "public" / "ready" / f"{session}.json", ready)
        for tester, blockers in results.items():
            store.append_jsonl(
                places.state / "qa-results" / f"{session}.jsonl",
                {
                    "head": head,
                    "tester": tester,
                    "agent": "a",
                    "at": iso(now - 600),
                    "blockers": blockers,
                    "majors": 0,
                    "minors": 0,
                },
            )
        for tester, shot in shots.items():
            run = "20261009T120000Z-abc123"
            folder = places.evidence / task / head[:12] / tester / run
            folder.mkdir(parents=True)
            source = FIXTURES / "pictures" / f"evidence-{task}-{shot}.svg"
            (folder / f"{shot}.svg").write_bytes(source.read_bytes())
            store.append_jsonl(
                places.evidence / "index.jsonl",
                {
                    "task": task,
                    "head": head,
                    "tester": tester,
                    "run": run,
                    "session": session,
                    "at": iso(now),
                    "files": [f"{shot}.svg"],
                },
            )


def _transcript(places: Places, now: float) -> None:
    """The sign-in loop's transcript, where the dashboard reads the owner's thread with it: a prompt that is not the
    owner's, then one message from the owner and the agent's two replies."""
    folder = places.state / "agent-home" / ".claude" / "projects" / "-srv-wt-impl-sign-in-loop"
    folder.mkdir(parents=True)

    def say(kind: str, back: int, content: Any) -> str:
        stamp = datetime.fromtimestamp(now - back, UTC).strftime("%Y-%m-%dT%H:%M:%S.123Z")
        return json.dumps({"type": kind, "timestamp": stamp, "message": {"role": kind, "content": content}})

    lines = [
        say("user", 900, "Task: t-0017"),
        say("assistant", 880, [{"type": "text", "text": "Starting on the sign-in loop."}]),
        say("user", 600, messages.PREFIX + "Check Safari first: most of the reports come from it."),
        say("assistant", 590, [{"type": "text", "text": "Safari drops the session cookie on the redirect."}]),
        say(
            "assistant", 580, [{"type": "tool_use", "name": "Bash"}, {"type": "text", "text": "A test now covers it."}]
        ),
    ]
    (folder / "impl-sign-in-loop.jsonl").write_text("\n".join(lines) + "\n")
