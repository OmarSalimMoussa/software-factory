from __future__ import annotations

import json
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from warden_fakes import HAIKU, OPUS

from runtime.shared import store
from runtime.shared.models import RETIRED, WORKING, Session
from runtime.shared.paths import Places
from runtime.warden.context import Context
from runtime.warden.sessions import registry
from runtime.warden.spend import cost


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


def test_a_copy_of_a_session_counts_each_call_once_its_originals_last_ones_too(ctx: Context, root: Path) -> None:
    folder = root / "-srv-checkout-wt-s1"
    registry.write(ctx.places, {"s0": session("s0", worktree="/srv/checkout/wt-s1")})
    (folder / "s0.jsonl").write_text(call("msg_1", "req_1", out=10))
    cost.run_pass(ctx, [root])
    with (folder / "s0.jsonl").open("a") as out:
        out.write(call("msg_2", "req_2", out=20))
    # The original's tester, still unread: it reported after the last pass, as the original stopped.
    (folder / "s0" / "subagents").mkdir(parents=True)
    (folder / "s0" / "subagents" / "agent-qa1.jsonl").write_text(call("msg_4", "req_4", out=40))
    copy = session("s1", renewed="2026-10-09T10:30:00Z", copied_from="s0")
    registry.write(ctx.places, {"s1": copy})
    # The copy's transcript repeats the original's lines under their first times, then its own.
    later = call("msg_3", "req_3", out=30).replace("10:01:02.345Z", "10:30:05.000Z")
    (folder / "s1.jsonl").write_text(call("msg_1", "req_1", out=10) + call("msg_2", "req_2", out=20) + later)

    cost.run_pass(ctx, [root])
    cost.run_pass(ctx, [root])

    assert sorted(r["output"] for r in records(ctx.places)) == [10, 20, 30, 40]


def test_an_unpriced_model_costs_unknown_and_a_failed_call_is_recorded(ctx: Context, root: Path) -> None:
    registry.write(ctx.places, {"s1": session("s1", state=RETIRED)})
    failed = call("err_1", "req_9", model="<synthetic>", out=0, isApiErrorMessage=True)
    (root / "-srv-checkout-wt-s1" / "s1.jsonl").write_text(call("msg_1", "req_1", model=HAIKU) + failed)

    cost.run_pass(ctx, [root])

    assert [(r["model"], r["usd"], r["failed"]) for r in records(ctx.places)] == [
        (HAIKU, None, False),
        (HAIKU, None, True),
    ]


def test_a_call_whose_counts_sit_in_its_parts_is_counted_from_them_and_one_that_counted_nothing_costs_unknown(
    ctx: Context, root: Path
) -> None:
    registry.write(ctx.places, {"s1": session("s1")})
    zero = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    part = {**zero, "output_tokens": 4427, "cache_read_input_tokens": 375722, "cache_creation_input_tokens": 4514}
    parted, empty = json.loads(call("msg_1", "req_1")), json.loads(call("msg_2", "req_2"))
    parted["message"]["usage"] = {**zero, "iterations": [part]}
    empty["message"]["usage"] = zero
    (root / "-srv-checkout-wt-s1" / "s1.jsonl").write_text(json.dumps(parted) + "\n" + json.dumps(empty) + "\n")

    cost.run_pass(ctx, [root])

    got = [(r["output"], r["cache_read"], r["usd"] is None) for r in records(ctx.places)]
    assert got == [(4427, 375722, False), (0, 0, True)]


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


def test_every_model_the_factory_allows_out_of_the_box_has_a_price_so_no_call_costs_unknown() -> None:
    shipped = tomllib.loads((Path(__file__).resolve().parents[2] / "settings.toml").read_text())

    assert set(shipped["prices"]) == set(shipped["models"]["allowed"])


def _line(model: str, text: str, at: str = "2026-10-09T12:00:05Z", **extra: object) -> str:
    message = {"model": model, "content": [{"type": "text", "text": text}], "usage": {}}
    return json.dumps({"type": "assistant", "timestamp": at, "message": message, **extra})


@pytest.mark.parametrize(
    ("line", "outcome"),
    [
        (_line("claude-opus-5-5", "Carrying on."), "ok"),
        (_line("<synthetic>", "API Error: 401 OAuth token has expired", isApiErrorMessage=True),
         "API Error: 401 OAuth token has expired"),
        (_line("<synthetic>", "Claude AI usage limit reached|1791000000", isApiErrorMessage=True), "limit"),
        (_line("claude-opus-5-5", "Carrying on.", at="2026-10-09T11:59:00Z"), None),
        ('{"type": "user"', None),
    ],
    ids=["answered", "no-sign-in", "plan-limit", "before-the-switch", "half-written"],
)  # fmt: skip
def test_a_calls_outcome_after_a_moment_says_whether_it_signed_in(line: str, outcome: str | None) -> None:
    since = datetime(2026, 10, 9, 12, 0, tzinfo=UTC).timestamp()
    assert cost.call_outcome(line, since) == outcome
