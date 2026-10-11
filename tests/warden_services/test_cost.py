from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from runtime.shared import settings, store
from runtime.shared.models import RETIRED, WORKING, Session
from runtime.shared.paths import Places
from runtime.warden.context import Context
from runtime.warden.sessions import registry
from runtime.warden.spend import cost

OPUS, HAIKU = "claude-opus-5-5", "claude-haiku-5-5"
PRICE = {"input": 15.0, "output": 75.0, "cache_read": 1.5, "cache_write": 18.75}


def session(sid: str, **fields: Any) -> Session:
    base: dict[str, Any] = dict(id=sid, name=f"impl-{sid}", role="implementer", family="impl", model=OPUS,
                                effort="high", task="t-0007", parent="", worktree="/srv/checkout/wt-" + sid,
                                state=WORKING, started="2026-10-09T10:00:00Z", started_by="orchestrator")  # fmt: skip
    return Session(**{**base, **fields})


def call(message_id: str, request_id: str, model: str = OPUS, out: int = 10, **extra: Any) -> str:
    usage = {"input_tokens": 1000, "output_tokens": out, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    line = {"type": "assistant", "requestId": request_id, "timestamp": "2026-10-09T10:01:02.345Z",
            "message": {"id": message_id, "model": model, "usage": usage}, **extra}  # fmt: skip
    return json.dumps(line) + "\n"


@pytest.fixture
def root(places: Places, tmp_path: Path) -> Path:
    places.overrides.parent.mkdir(parents=True)
    places.overrides.write_text(settings.overrides_toml({"prices": {OPUS: PRICE}}))
    root = tmp_path / "projects"
    (root / "-srv-checkout-wt-s1").mkdir(parents=True)
    return root


def records(places: Places) -> list[dict[str, Any]]:
    return store.read_jsonl(places.cost)


def test_one_call_written_on_several_lines_is_counted_once(ctx: Context, root: Path) -> None:
    registry.write(ctx.places, {"s1": session("s1")})
    path = root / "-srv-checkout-wt-s1" / "s1.jsonl"
    # Claude Code writes a reply's thinking, text and tool use as adjacent lines repeating its usage; the last line of
    # a pass may still be half written.
    path.write_text(call("msg_1", "req_1") + call("msg_1", "req_1") + '{"type": "assist')
    cost.run_pass(ctx, [root])
    with path.open("a") as out:
        out.write('ant"}\n' + call("msg_1", "req_1") + call("msg_2", "req_2", out=20))
    cost.run_pass(ctx, [root])
    cost.run_pass(ctx, [root])

    got = records(ctx.places)
    assert [(r["output"], r["usd"], r["task"], r["at"]) for r in got] == [
        (10, 0.01575, "t-0007", "2026-10-09T10:01:02Z"),
        (20, 0.0165, "t-0007", "2026-10-09T10:01:02Z"),
    ]
    assert cost.spent_days(ctx) == {"2026-10-09": [pytest.approx(0.03225), 0]}  # what the budget gate reads


def test_an_unpriced_model_costs_unknown_and_a_failed_call_is_recorded(ctx: Context, root: Path) -> None:
    registry.write(ctx.places, {"s1": session("s1", state=RETIRED)})
    failed = call("err_1", "req_9", model="<synthetic>", out=0, isApiErrorMessage=True)
    (root / "-srv-checkout-wt-s1" / "s1.jsonl").write_text(call("msg_1", "req_1", model=HAIKU) + failed)

    cost.run_pass(ctx, [root])

    assert [(r["model"], r["usd"], r["failed"]) for r in records(ctx.places)] == [
        (HAIKU, None, False),
        (HAIKU, None, True),
    ]


@pytest.mark.parametrize(
    ("kind", "row", "role", "family", "task"),
    [
        ("qa-backend", None, "qa-backend", "qa", "t-0007"),
        ("triage", None, "triage", "intake", cost.UPKEEP),
        ("intake", {"role": "intake", "family": "intake", "task": "r-2026-10-09-0042"}, "intake", "intake",
         "r-2026-10-09-0042"),
    ],
)  # fmt: skip
def test_a_subagent_call_is_charged_by_its_kind(
    ctx: Context, root: Path, kind: str, row: dict[str, Any] | None, role: str, family: str, task: str
) -> None:
    sessions = {"s1": session("s1")}
    if row is not None:
        sessions["a1"] = session("a1", parent="s1", agent_id="abc", worktree="", model=OPUS, **row)
    registry.write(ctx.places, sessions)
    folder = root / "-srv-checkout-wt-s1" / "s1" / "subagents"
    folder.mkdir(parents=True)
    (root / "-srv-checkout-wt-s1" / "s1.jsonl").write_text("")
    (folder / "agent-abc.jsonl").write_text(call("msg_9", "req_9"))
    (folder / "agent-abc.meta.json").write_text(json.dumps({"agentType": kind}))

    cost.run_pass(ctx, [root])

    [record] = records(ctx.places)
    assert (record["session"], record["subagent"], record["role"], record["family"], record["task"]) == (
        "s1", "abc", role, family, task,
    )  # fmt: skip
