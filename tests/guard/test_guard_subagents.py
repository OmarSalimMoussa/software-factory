"""Subagents: which kinds, on which model, how many at once with a place kept for intake, a tester's brief, QA only
on a ready head and within its rounds, critic rounds, a tester's time, the implementer's check runs, no finished
subagent resumed, and every brief ending with the note on reflecting."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from guard_kit import NOW, TESTER_ID, Kit, agent_use, bash_use, entry, result

from runtime.guard.facts import Refusal, Rewrite
from runtime.guard.subagents import SLOT_API
from runtime.shared import store

TESTER = {"agent_id": TESTER_ID, "agent_type": "qa-backend"}


def _agent(kind: str, prompt: str = "Scenarios and checks: 1", **more: Any) -> dict[str, Any]:
    return {"subagent_type": kind, "prompt": prompt, **more}


@pytest.mark.parametrize(
    ("role", "kind", "extra", "rule"),
    [
        ("implementer", "qa-backend", {}, None),
        ("implementer", "critic-ux", {}, "subagent-kinds"),
        ("designer", "critic-ux", {}, None),
        ("orchestrator", "triage", {}, None),
        ("orchestrator", "general-purpose", {}, "subagent-kinds"),
        ("implementer", "Explore", {"agent_id": TESTER_ID, "agent_type": "qa-backend"}, "subagent-kinds"),
    ],
)
def test_a_role_starts_only_its_kinds_and_a_subagent_starts_none(
    tmp_path: Path, role: str, kind: str, extra: dict[str, str], rule: str | None
) -> None:
    kit = Kit(tmp_path)
    kit.session(role)
    kit.subagent(TESTER_ID, [entry(NOW, "user", "Scenarios and checks: 2")])
    kit.switch_off("qa-ready")
    assert kit.rule("Agent", _agent(kind), **extra) == rule


@pytest.mark.parametrize(
    ("kind", "model", "refused"),
    [
        ("qa-backend", "", False),
        ("qa-backend", "sonnet", False),
        ("qa-backend", "claude-sonnet-5-5", False),
        ("qa-backend", "opus", True),
        ("critic-security", "sonnet", False),
        ("critic-security", "haiku", True),
        ("Explore", "haiku", True),
    ],
)
def test_an_agent_call_asks_for_its_kinds_configured_model_or_none(
    tmp_path: Path, kind: str, model: str, refused: bool
) -> None:
    kit = Kit(tmp_path)
    kit.session("designer" if kind.startswith("critic-") else "implementer")
    kit.settings(subagents__designer=["critic-security", "Explore"])
    kit.switch_off("qa-ready")
    assert (kit.rule("Agent", _agent(kind, model=model)) == "model") is refused


def _running(*kinds: str, ago: int = 30) -> list[str]:
    return [entry(NOW - timedelta(seconds=ago), "assistant", [agent_use(f"t{i}", k) for i, k in enumerate(kinds)])]


@pytest.mark.parametrize(
    ("running", "kind", "refused"),
    [
        (["qa-regression", "qa-regression"], "qa-prod", False),
        (["qa-regression", "qa-regression", "qa-regression"], "qa-prod", True),
        (["qa-regression", "qa-regression", "qa-regression"], "intake", False),
        (["qa-regression", "qa-regression", "qa-regression", "intake"], "intake", True),
    ],
)
def test_the_orchestrator_keeps_one_place_for_intake(
    tmp_path: Path, running: list[str], kind: str, refused: bool
) -> None:
    kit = Kit(tmp_path)
    kit.session("orchestrator", worktree=kit.repo)
    kit.transcript(_running(*running))
    assert (kit.rule("Agent", _agent(kind)) == "subagents-at-once") is refused


@pytest.mark.parametrize(("current", "refused"), [("t0", False), ("t3", False), ("t4", True)])
def test_a_batch_of_starts_past_the_cap_refuses_only_the_starts_past_it(
    tmp_path: Path, current: str, refused: bool
) -> None:
    kit = Kit(tmp_path)
    kit.session("designer")
    kit.settings(caps__subagents_at_once=4)
    # Five critics started in one message: each one's hook saw the other four as running, and all five were refused.
    kit.transcript(_running("critic-architecture", "critic-reliability", "critic-ux", "critic-product",
                            "critic-security", ago=0))  # fmt: skip
    assert (kit.rule("Agent", _agent("critic-security"), tool_use_id=current) == "subagents-at-once") is refused


def test_a_quiet_tester_holds_its_place_through_its_start_up_and_items(tmp_path: Path) -> None:
    kit = Kit(tmp_path)
    kit.session("implementer")
    kit.settings(caps__subagents_at_once=1, qa__setup_seconds=180, qa__seconds_per_item=120, qa__stale_seconds=600)
    kit.transcript(_running("qa-backend", ago=180 + 120 - 30))
    assert kit.rule("Agent", _agent("qa-security")) == "subagents-at-once"
    kit.transcript(_running("qa-backend", ago=180 + 120 + 30))
    assert kit.rule("Agent", _agent("qa-security")) != "subagents-at-once"


def test_a_subagent_quiet_past_the_stale_limit_or_stopped_frees_its_place(tmp_path: Path) -> None:
    kit = Kit(tmp_path)
    kit.session("designer")
    kit.settings(caps__subagents_at_once=2, qa__stale_seconds=120)
    kit.transcript(_running("critic-ux", "critic-product"))
    assert kit.rule("Agent", _agent("critic-security")) == "subagents-at-once"
    kit.transcript(_running("critic-ux", "critic-product", ago=121))
    assert kit.rule("Agent", _agent("critic-security")) is None
    launched = result("t0", f"Async agent launched successfully. agentId: {TESTER_ID}")
    kit.transcript([*_running("critic-ux", "critic-product"), entry(NOW, "user", [launched])])
    kit.session("designer", sid="sub-1", state="retired", agent_id=TESTER_ID)
    assert kit.rule("Agent", _agent("critic-security")) is None


@pytest.mark.parametrize(
    "prompt", ["walk it", "Scenarios and checks: 11", "Scenarios and checks: 1\nScenarios and checks: 2"]
)
def test_a_tester_brief_declares_its_scenarios_once(tmp_path: Path, prompt: str) -> None:
    kit = Kit(tmp_path)
    kit.session("orchestrator", worktree=kit.repo)
    assert kit.rule("Agent", _agent("qa-prod", prompt)) == "tester-brief"


def test_qa_waits_for_a_ready_record_of_the_exact_head(tmp_path: Path) -> None:
    kit = Kit(tmp_path)
    kit.git_product()
    kit.session("implementer")
    assert kit.rule("Agent", _agent("qa-backend")) == "qa-ready"
    kit.ready({"head": "0" * 40, "slices": [], "at": NOW.isoformat()})
    assert kit.rule("Agent", _agent("qa-backend")) == "qa-ready"
    kit.ready({"head": kit.head(), "slices": [], "at": NOW.isoformat()})
    assert kit.rule("Agent", _agent("qa-backend")) is None


@pytest.mark.parametrize(("extended", "rule"), [(0, "qa-rounds"), (1, None)])
def test_a_fifth_head_of_testers_is_refused_unless_extended(tmp_path: Path, extended: int, rule: str | None) -> None:
    kit = Kit(tmp_path)
    kit.git_product()
    kit.session("implementer", extensions={"qa": extended})
    lines, records = [], []
    for i in range(4):
        at = NOW - timedelta(hours=5 - i)
        records.append({"head": str(i) * 40, "slices": [], "at": at.isoformat()})
        lines.append(entry(at + timedelta(minutes=1), "assistant", [agent_use(f"t{i}", "qa-backend")]))
        lines.append(entry(at + timedelta(minutes=2), "user", [result(f"t{i}", "done")]))
    kit.ready(*records, {"head": kit.head(), "slices": [], "at": NOW.isoformat()})
    kit.transcript(lines)
    assert kit.rule("Agent", _agent("qa-backend")) == rule


@pytest.mark.parametrize(("interrupted", "rule"), [(False, "qa-rounds"), (True, None)])
def test_a_qa_round_cut_short_by_a_bench_does_not_count(tmp_path: Path, interrupted: bool, rule: str | None) -> None:
    kit = Kit(tmp_path)
    kit.git_product()
    kit.session("implementer")
    if interrupted:
        kit.session("implementer", sid="sub-1", state="retired", agent_id=TESTER_ID, retired="with-parent")
    lines, records = [], []
    for i in range(4):
        at = NOW - timedelta(hours=5 - i)
        records.append({"head": str(i) * 40, "slices": [], "at": at.isoformat()})
        lines.append(entry(at + timedelta(minutes=1), "assistant", [agent_use(f"t{i}", "qa-backend")]))
        done = f"Async agent launched successfully. agentId: {TESTER_ID}" if i == 3 else "done"
        lines.append(entry(at + timedelta(minutes=2), "user", [result(f"t{i}", done)]))
    kit.ready(*records, {"head": kit.head(), "slices": [], "at": NOW.isoformat()})
    kit.transcript(lines)
    assert kit.rule("Agent", _agent("qa-backend")) == rule


def test_a_critic_round_is_the_critics_started_between_two_saved_revisions(tmp_path: Path) -> None:
    kit = Kit(tmp_path)
    kit.session("designer")
    lines = []
    for i in range(4):
        at = NOW - timedelta(hours=5 - i)
        lines.append(entry(at, "assistant", [agent_use(f"c{i}", "critic-ux"), agent_use(f"d{i}", "critic-product")]))
        lines.append(entry(at, "user", [result(f"c{i}", "done"), result(f"d{i}", "done")]))
        lines.append(entry(at + timedelta(minutes=5), "assistant", [bash_use(f"s{i}", "factory spec save t-1 s.md")]))
    kit.transcript(lines)
    assert kit.rule("Agent", _agent("critic-security")) == "critic-rounds"
    kit.transcript(lines[:-1])
    assert kit.rule("Agent", _agent("critic-security")) is None
    # The fourth round was cut short by a bench, so after the save this is the fourth round again.
    lines[-3] = entry(NOW - timedelta(hours=2), "assistant", [agent_use("c3", "critic-ux")])
    lines[-2] = entry(NOW - timedelta(hours=2), "user", [result("c3", f"Async agent launched. agentId: {TESTER_ID}")])
    kit.transcript(lines)
    kit.session("designer", sid="sub-1", state="retired", agent_id=TESTER_ID, retired="with-parent")
    assert kit.rule("Agent", _agent("critic-security")) is None


def test_a_testers_time_runs_out_and_its_shell_timeouts_are_cut_but_its_report_goes(tmp_path: Path) -> None:
    kit = Kit(tmp_path)
    kit.session("implementer")
    kit.settings(qa__setup_seconds=30, qa__seconds_per_item=120)
    kit.subagent(TESTER_ID, [entry(NOW - timedelta(seconds=130), "user", "Scenarios and checks: 1")])
    cut = kit.decide("Bash", {"command": "sleep 600", "timeout": 600_000}, **TESTER)
    assert isinstance(cut, Rewrite) and cut.updated_input["timeout"] <= 20_000
    kit.subagent(TESTER_ID, [entry(NOW - timedelta(seconds=160), "user", "Scenarios and checks: 1")])
    refused = kit.decide("Read", {"file_path": "/x"}, **TESTER)
    assert isinstance(refused, Refusal) and "factory qa result" in refused.reason
    assert kit.decide("SubagentHandback", {"message": "2 of 3"}, **TESTER) is None
    walker = kit.decide("Read", {"file_path": "/x"}, agent_id=TESTER_ID, agent_type="qa-regression")
    assert isinstance(walker, Refusal) and walker.reason.endswith("hand back your report now")


def test_a_testers_slot_api_session_carries_its_id_and_the_implementers_keeps_its_name(tmp_path: Path) -> None:
    kit = Kit(tmp_path)
    kit.session("implementer")
    kit.subagent(TESTER_ID, [entry(NOW, "user", "Scenarios and checks: 1")])
    asked = {"method": "GET", "path": "/me", "session": "ada@notes.test"}
    tester = kit.decide(SLOT_API, asked, **TESTER)
    assert isinstance(tester, Rewrite) and tester.updated_input["session"] == f"ada@notes.test.{TESTER_ID}"
    assert kit.decide(SLOT_API, asked) is None


@pytest.mark.parametrize(
    ("tool", "tool_input", "goes"),
    [
        (
            "Bash",
            {"command": "factory qa finding --head abc1234 --severity minor --where x --body .factory/f.md"},
            True,
        ),
        ("Bash", {"command": "factory qa result --head abc1234"}, True),
        ("Write", {"file_path": ".factory/f.md", "content": "Save fails."}, True),
        ("Write", {"file_path": "notes.md", "content": "Save fails."}, False),
        ("Bash", {"command": "factory qa result --head abc1234; sleep 60"}, False),
        ("Bash", {"command": "factory qa step --head abc1234"}, False),
        ("mcp__qa-browser__browser_snapshot", {}, False),
    ],
    ids=["finding", "result", "finding-file", "other-file", "result-and-more", "step", "browser"],
)
def test_a_tester_out_of_time_still_records_its_report(
    tmp_path: Path, tool: str, tool_input: dict[str, Any], goes: bool
) -> None:
    kit = Kit(tmp_path)
    kit.session("implementer")
    kit.subagent(TESTER_ID, [entry(NOW - timedelta(hours=1), "user", "Scenarios and checks: 1")])
    asked = {**tool_input, "file_path": str(kit.worktree / tool_input["file_path"])} if tool == "Write" else tool_input
    assert (kit.rule(tool, asked, **TESTER) != "tester-time") is goes


@pytest.mark.parametrize(
    ("leased", "tool", "refused"),
    [
        (True, "mcp__computer-use__screenshot", True),
        (True, "Read", True),
        (False, "mcp__qa-browser__browser_mouse_click_xy", False),  # a mouse step on a web page is no desktop use
        (False, "Read", False),
    ],
)
def test_a_tester_holding_a_desktop_stops_at_its_desktop_time(
    tmp_path: Path, leased: bool, tool: str, refused: bool
) -> None:
    kit = Kit(tmp_path)
    kit.session("implementer")
    kit.subagent(TESTER_ID, [entry(NOW - timedelta(seconds=310), "user", "Scenarios and checks: 10")])
    store.write_json(kit.public / "desktops.json", {"2": TESTER_ID} if leased else {})
    tester = {"agent_id": TESTER_ID, "agent_type": "qa-ui-function"}
    assert (kit.rule(tool, {"file_path": "/x"}, **tester) == "tester-time") is refused


@pytest.mark.parametrize(
    ("running", "age", "level", "refused"),
    [
        (3, 60, "green", False),
        (4, 60, "green", True),
        (4, 10 * 120 + 180 + 300 - 60, "green", True),
        (4, 86400, "green", False),
        (0, 60, "amber", True),
    ],
    ids=[
        "under-the-cap",
        "at-the-cap",
        "rows-within-a-testers-longest-run",
        "rows-older-than-any-tester-runs",
        "amber",
    ],
)
def test_browser_testers_are_capped_across_the_machine_and_start_only_on_green_vitals(
    tmp_path: Path, running: int, age: int, level: str, refused: bool
) -> None:
    kit = Kit(tmp_path)
    kit.session("implementer")
    kit.settings(qa__max_items=10, qa__seconds_per_item=120, qa__setup_seconds=180, qa__stale_seconds=300)
    for n in range(running):  # other implementers' browser testers
        kit.session("implementer", sid=f"other-{n}")
        kit.session("qa-ux", sid=f"tester-{n}", parent=f"other-{n}", agent_id=f"a{n}",
                    started=(NOW - timedelta(seconds=age)).isoformat())  # fmt: skip
    why = "" if level == "green" else "power: on battery, 25%"
    store.write_json(kit.public / "vitals.json", {"level": level, "why": why})
    brief = "Scenarios and checks: 1\nWalk the sign-in."
    decision = kit.decide("Agent", {"subagent_type": "qa-ux", "prompt": brief})
    refusal = decision if isinstance(decision, Refusal) else None
    assert (refusal is not None and refusal.rule == "subagents-at-once") is refused
    assert refusal is None or why in refusal.reason  # it names the reading that holds the tester back


@pytest.mark.parametrize("kind", ["intake", "Explore", "qa-backend"])
def test_a_finished_subagent_of_any_kind_is_not_resumed(tmp_path: Path, kind: str) -> None:
    kit = Kit(tmp_path)
    kit.session("implementer")
    kit.transcript(
        [
            entry(NOW - timedelta(minutes=3), "assistant", [agent_use("t1", kind)]),
            entry(NOW - timedelta(minutes=3), "user", [result("t1", f"Async agent launched. agentId: {TESTER_ID}")]),
            entry(NOW - timedelta(minutes=1), "user", f'<agent-message from="{TESTER_ID}">\n[Subagent hand-back]'),
        ]
    )
    assert kit.rule("SendMessage", {"to": TESTER_ID, "message": "one more"}) == "resume-subagent"


@pytest.mark.parametrize(
    ("role", "kind", "may"),
    [
        ("implementer", "qa-backend", True),
        ("designer", "critic-security", True),
        ("orchestrator", "triage", True),
        ("implementer", "Explore", False),
    ],
)
def test_a_brief_carries_the_notes_on_calling_factory_and_reflecting_exactly_when_its_kind_may(
    tmp_path: Path, role: str, kind: str, may: bool
) -> None:
    kit = Kit(tmp_path)
    where = kit.repo if role == "orchestrator" else kit.worktree
    kit.session(role, worktree=where)
    kit.switch_off("qa-ready")
    decision = kit.decide("Agent", _agent(kind))
    noted = isinstance(decision, Rewrite) and decision.updated_input["prompt"].endswith(
        "Write to SELF_REFLECT.md when a miss got past you.\n"
    )
    assert noted is may
    told = isinstance(decision, Rewrite) and "Run factory on its own." in decision.updated_input["prompt"]
    assert told is may
    if noted:
        assert isinstance(decision, Rewrite)
        assert not isinstance(kit.decide("Agent", decision.updated_input), Refusal)
    kit.subagent(TESTER_ID, [entry(NOW, "user", "Scenarios and checks: 1")])
    reflect = kit.bash('factory reflect --kind friction "x"', cwd=str(where), agent_id=TESTER_ID, agent_type=kind)
    assert (reflect is None) is may


def test_intakes_time_runs_out_and_then_only_factory_and_its_hand_back_go(tmp_path: Path) -> None:
    kit = Kit(tmp_path)
    kit.session("orchestrator")
    intake = {"agent_id": TESTER_ID, "agent_type": "intake"}
    limit = 20 * 60
    kit.settings(intake__fast_lane_minutes=20)
    kit.subagent(TESTER_ID, [entry(NOW - timedelta(seconds=limit - 10), "user", "Request r-1")])
    assert kit.rule("Read", {"file_path": "/x"}, **intake) is None

    kit.subagent(TESTER_ID, [entry(NOW - timedelta(seconds=limit + 10), "user", "Request r-1")])

    assert kit.rule("Read", {"file_path": "/x"}, **intake) == "intake-time"
    assert kit.rule("Bash", {"command": "factory task facts t-0001 --body facts.json"}, **intake) is None
    assert kit.decide("SubagentHandback", {"message": "placed"}, **intake) is None
