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
    kit.subagent(TESTER_ID, [entry(NOW - timedelta(seconds=100), "user", "Scenarios and checks: 1")])
    cut = kit.decide("Bash", {"command": "sleep 600", "timeout": 600_000}, **TESTER)
    assert isinstance(cut, Rewrite) and cut.updated_input["timeout"] <= 20_000
    kit.subagent(TESTER_ID, [entry(NOW - timedelta(seconds=130), "user", "Scenarios and checks: 1")])
    assert kit.rule("Read", {"file_path": "/x"}, **TESTER) == "tester-time"
    assert kit.decide("SubagentHandback", {"message": "2 of 3"}, **TESTER) is None


@pytest.mark.parametrize(
    ("used", "tool", "refused"),
    [
        (None, "mcp__computer-use__screenshot", True),
        (None, "mcp__qa-browser__browser_mouse_click_xy", True),
        ("mcp__computer-use__left_click", "Read", True),
        (None, "mcp__qa-browser__browser_click", False),
        (None, "Read", False),
    ],
)
def test_a_tester_on_the_remote_desktop_stops_at_its_desktop_time(
    tmp_path: Path, used: str | None, tool: str, refused: bool
) -> None:
    kit = Kit(tmp_path)
    kit.session("implementer")
    lines = [entry(NOW - timedelta(seconds=310), "user", "Scenarios and checks: 10")]
    if used:
        lines.append(entry(NOW - timedelta(seconds=200), "assistant", [{"type": "tool_use", "id": "x", "name": used}]))
    kit.subagent(TESTER_ID, lines)
    tester = {"agent_id": TESTER_ID, "agent_type": "qa-ui-function"}
    assert (kit.rule(tool, {"file_path": "/x"}, **tester) == "tester-time") is refused


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


def test_every_brief_ends_with_the_note_on_reflecting(tmp_path: Path) -> None:
    kit = Kit(tmp_path)
    kit.session("implementer")
    kit.switch_off("qa-ready")
    decision = kit.decide("Agent", _agent("qa-backend"))
    assert isinstance(decision, Rewrite)
    assert decision.updated_input["prompt"].endswith("Write to SELF_REFLECT.md when a miss got past you.\n")
    assert not isinstance(kit.decide("Agent", decision.updated_input), Refusal)


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
