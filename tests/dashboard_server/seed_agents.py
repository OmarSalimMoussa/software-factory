"""The afternoon's agents as the warden keeps them: the registry and its history, each call's cost, QA evidence on
two tasks, and one transcript.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from seed_kit import FIXTURES, MODELS, fixture, fixture_time, iso
from seed_replay import seed_replay

from runtime.shared import store
from runtime.shared.models import CostRecord, Session, Task
from runtime.shared.paths import Places
from runtime.warden.owner import messages

COSTS: dict[str, float] = {}  # each agent's cost as the fixtures show it, filled as the agents are written
REPLAYED = ("t-0017", "qa-ui-function")  # the one run whose replay is built
REPLAY_URL = "/api/replay?task=t-0017&head=eeeeeeeeeeee&tester=qa-ui-function&run=20261009T120000Z-abc123"


def _usd(text: str) -> float:
    return float(text.lstrip("$").replace(",", ""))


def _nodes(node: dict[str, Any]) -> list[dict[str, Any]]:
    return [node, *(n for kid in node.get("kids", []) for n in _nodes(kid))]


def history_lines(sessions: list[Session]) -> str:
    """What the lifecycle writes: a line as each agent starts and as it retires; and one line someone broke."""
    lines = [{"at": s.started, "session": s.id, "name": s.name, "state": "working", "retired": "", "by": s.started_by,
              "why": "started"} for s in sessions]  # fmt: skip
    lines += [{"at": s.ended, "session": s.id, "name": s.name, "state": "retired", "retired": s.retired,
               "by": s.retired_by or "warden", "why": f"{s.retired} by the {s.retired_by or 'warden'}"}
              for s in sessions if s.state == "retired"]  # fmt: skip
    text = [json.dumps(line, sort_keys=True) for line in sorted(lines, key=lambda line: line["at"])]
    return "\n".join([*text[:5], '{"at": "2026-10-0', *text[5:]]) + "\n"


def seed_sessions(now: float) -> list[Session]:
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
    roles = {"implementer, small": "implementer", "implementer, large": "implementer"}
    out = []

    def walk(node: dict[str, Any], parent: str, minutes: int) -> None:
        role = roles.get(node["role"], node["role"])
        if role in ("tester", "critic"):
            role = node["name"]
        state = {"accent": "working", "warn": "waiting", "depri": "deprioritized", "good": "retired"}[node["dot"]]
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
                state=state,
                started=iso(now - minutes * 60),
                started_by="orchestrator",
                doing=node["doing"] if role.startswith(("qa-", "critic-", "intake")) else "",
                retired="finished" if state == "retired" else "",
                ended=iso(now - 60) if state == "retired" else "",
                note="benched for t-0021" if state == "deprioritized" else "",
                benched_for="t-0021" if state == "deprioritized" else "",
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
        started, ended = fixture_time(h["started"], now - 3600), fixture_time(h["ended"], now - 1800)
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


def seed_cost(sessions: list[Session], tasks: list[Task], now: float) -> list[CostRecord]:
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
                    cache_read=40000,
                    cache_write=2000,
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
                    cache_read=80000,
                    cache_write=4000,
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


def seed_evidence(places: Places, now: float) -> None:
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
            if (task, tester) == REPLAYED:
                seed_replay(folder)
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


def seed_transcript(places: Places, now: float) -> None:
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
