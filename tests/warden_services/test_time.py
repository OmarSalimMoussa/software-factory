from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_cost import session

from runtime.shared import store
from runtime.shared.models import Session
from runtime.shared.paths import Places
from runtime.warden import upgrade
from runtime.warden.context import Context
from runtime.warden.sessions import registry
from runtime.warden.spend import cost, partition


def line(second: float, kind: str, *blocks: dict[str, Any], reply: str = "", **extra: Any) -> str:
    stamp = f"2026-10-09T10:{int(second) // 60:02d}:{second % 60:06.3f}Z"
    message: dict[str, Any] = {"content": list(blocks) or "go on"}
    if kind == "assistant":
        message.update(id=reply, model="claude-opus-5-5")
    return json.dumps({"type": kind, "timestamp": stamp, "message": message, **extra}) + "\n"


def use(ident: str, name: str, **given: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": ident, "name": name, "input": given}


def done(ident: str) -> dict[str, Any]:
    return {"type": "tool_result", "tool_use_id": ident, "content": "secret output"}


THINK = {"type": "thinking", "thinking": "..."}
FIRST_PASS = (
    line(0, "user")
    + line(10, "assistant", THINK, reply="A")
    + line(12, "assistant", use("T1", "Bash", command="cd /srv/x && factory check --only home-loads"), reply="A")
    + line(40, "user", done("T1"))
    + line(45, "assistant", use("T2", "Read", file_path="/srv/x/.env"), reply="B")
)
SECOND_PASS = (
    line(46, "assistant", use("T3", "Agent", prompt="walk it"), reply="B")
    + line(56, "user", done("T2"))
    + line(76, "user", done("T3"))
    + line(80, "assistant", {"type": "text", "text": "Done."}, reply="C")
    + line(380, "user")
    + json.dumps({"type": "system", "timestamp": "2026-10-09T10:06:21.000Z"})
    + "\n"
)


def times(places: Places) -> list[dict[str, Any]]:
    return store.read_jsonl(places.times)


def total(rows: list[dict[str, Any]]) -> dict[str, Any]:
    tools: dict[str, float] = {}
    for row in rows:
        for key, seconds in row["tools"].items():
            tools[key] = tools.get(key, 0) + seconds
    parts = {part: round(sum(r[part] for r in rows), 3) for part in ("model", "waiting", "idle")}
    return {**parts, "tools": tools, "first": rows[0]["first"], "last": rows[-1]["last"]}


def test_an_agents_time_divides_into_model_tools_waiting_and_idle_to_the_second_across_passes(
    ctx: Context, root: Path
) -> None:
    registry.write(ctx.places, {"s1": session("s1")})
    path = root / "-srv-checkout-wt-s1" / "s1.jsonl"
    path.write_text(FIRST_PASS + '{"type": "assist')
    cost.run_pass(ctx, [root])
    path.write_text(FIRST_PASS + SECOND_PASS)
    cost.run_pass(ctx, [root])
    cost.run_pass(ctx, [root])

    rows = times(ctx.places)
    assert len(rows) == 2
    assert total(rows) == {
        "model": 22.0,
        "waiting": 25.0,
        "idle": 300.0,
        "tools": {"Bash factory check --only": 28.0, "Read": 6.0},
        "first": "2026-10-09T10:00:00Z",
        "last": "2026-10-09T10:06:21Z",
    }
    assert {(r["session"], r["subagent"], r["task"], r["role"], r["whole"]) for r in rows} == {
        ("s1", "", "t-0007", "implementer", True)
    }


@pytest.mark.parametrize(
    ("name", "given", "key"),
    [
        ("Bash", {"command": "factory slot up 3"}, "Bash factory slot up"),
        ("Bash", {"command": "cd /srv/x && factory qa ready --head abc123"}, "Bash factory qa ready"),
        ("Bash", {"command": "git push origin main"}, "Bash git"),
        ("Bash", {"command": "/usr/bin/python3 -I scripts/x.py"}, "Bash python3"),
        ("Bash", {"command": "TOKEN=sk-live-123 curl -H x https://example.com"}, "Bash"),
        ("Bash", {"command": "$(cat /run/secret) --go"}, "Bash"),
        ("mcp__plugin_qa_browser__browser_take_screenshot", {"filename": "a.png"}, "browser_take_screenshot"),
    ],
)
def test_a_tool_is_keyed_by_its_name_and_a_command_by_its_verb_never_by_what_it_was_given(
    name: str, given: dict[str, Any], key: str
) -> None:
    assert partition.tool_key(name, given) == key


def _tester(sid: str, parent: Session, role: str) -> Session:
    return session(sid, parent=parent.id, agent_id=sid, worktree="", role=role, family="qa", task=parent.task)


@pytest.mark.parametrize(
    ("lead", "role", "task"),
    [
        (session("s1"), "qa-ui-function", "t-0007"),
        (session("s1", name="orchestrator", role="orchestrator", family="orch", task=""), "qa-regression", ""),
    ],
)
def test_a_subagents_time_goes_to_its_parents_task_kept_apart_from_its_parents_own(
    ctx: Context, root: Path, lead: Session, role: str, task: str
) -> None:
    registry.write(ctx.places, {"s1": lead, "abc": _tester("abc", lead, role)})
    folder = root / "-srv-checkout-wt-s1"
    (folder / "s1.jsonl").write_text(line(0, "user") + line(5, "assistant", reply="A"))
    (folder / "s1" / "subagents").mkdir(parents=True)
    (folder / "s1" / "subagents" / "agent-abc.jsonl").write_text(line(1, "user") + line(4, "assistant", reply="Z"))

    cost.run_pass(ctx, [root])

    got = {(r["session"], r["subagent"], r["role"], r["task"], r["model"]) for r in times(ctx.places)}
    assert got == {("s1", "", lead.role, task, 5.0), ("s1", "abc", role, task, 3.0)}


def test_a_transcript_read_before_time_was_measured_is_measured_from_where_it_stood_and_never_called_whole(
    ctx: Context, root: Path
) -> None:
    registry.write(ctx.places, {"s1": session("s1")})
    path = root / "-srv-checkout-wt-s1" / "s1.jsonl"
    path.write_text(line(0, "user") + line(9, "assistant", reply="A"))
    store.write_json(ctx.places.cost_cursor, {str(path): {"offset": path.stat().st_size, "last": "", "model": ""}})
    upgrade.migrate(ctx)
    with path.open("a") as out:
        out.write(line(600, "user") + line(602, "assistant", reply="B"))

    cost.run_pass(ctx, [root])

    [row] = times(ctx.places)
    assert (row["model"], row["idle"], row["first"], row["whole"]) == (2.0, 0.0, "2026-10-09T10:10:00Z", False)
