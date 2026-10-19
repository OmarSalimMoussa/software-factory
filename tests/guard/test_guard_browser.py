"""Driving the product and the owner's record: only UI testers drive, nobody drives the owner's Chrome, no code runs
in the browser server, and no agent opens the factory's state or the dashboard, by path, wildcard or address."""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from guard_kit import NOW, SID, TESTER_ID, Kit, entry

from runtime.guard.facts import Refusal, Rewrite

UI_TESTER = {"agent_id": TESTER_ID, "agent_type": "qa-ui-function"}


@pytest.fixture
def kit(tmp_path: Path) -> Kit:
    made = Kit(tmp_path)
    made.session("implementer")
    made.settings(
        dashboard__hosts=["127.0.0.1", "localhost", "laptop.tail1.ts.net"],
        qa__setup_seconds=60,
        qa__seconds_per_item=120,
    )
    made.subagent(TESTER_ID, [entry(NOW - timedelta(seconds=10), "user", "Scenarios and checks: 5")])
    return made


@pytest.mark.parametrize(
    ("tool", "tool_input", "tester", "refused"),
    [
        ("Read", {"file_path": "{evidence}/run/video/page@1.webm"}, False, True),
        ("Bash", {"command": "ls {evidence}"}, False, True),
        ("Bash", {"command": "rm -rf {evidence}/run"}, False, True),
        ("Glob", {"pattern": "**/*.png", "path": "{evidence}"}, True, True),
        ("mcp__qa-browser__browser_file_upload", {"paths": ["{evidence}/a.png"]}, True, True),
        ("Bash", {"command": "cp -r {state}/ev* /tmp/x"}, False, True),
        ("Bash", {"command": "ls {state}/*"}, False, True),
        ("Bash", {"command": "curl -s http://127.0.0.1:8300/api/home"}, False, True),
        ("WebFetch", {"url": "https://laptop.tail1.ts.net/", "prompt": "x"}, False, True),
        ("mcp__qa-browser__browser_navigate", {"url": "http://localhost:8300/"}, True, True),
        ("mcp__qa-browser__browser_navigate", {"url": "http://localhost:8101/"}, True, False),
        ("Bash", {"command": "curl -s http://localhost:8101/health"}, False, False),
        ("Bash", {"command": "cat {state}/public/sessions.json"}, False, False),
        ("Bash", {"command": "grep -rn evidence docs"}, False, False),
        ("Grep", {"pattern": "evidence", "path": "docs"}, False, False),
        ("SendMessage", {"to": "main", "message": "see {evidence}/run/page-1.png"}, False, False),
        ("Write", {"file_path": "{worktree}/notes.md", "content": "see {evidence}/run/page-1.png"}, False, False),
    ],
)
def test_the_evidence_rule_reads_paths_and_addresses_never_prose(
    kit: Kit, tool: str, tool_input: dict[str, Any], tester: bool, refused: bool
) -> None:
    where = {"evidence": kit.places.evidence, "state": kit.places.state, "worktree": kit.worktree}
    asked = {k: _fill(v, where) for k, v in tool_input.items()}
    assert (kit.rule(tool, asked, **(UI_TESTER if tester else {})) == "qa-evidence") is refused


def _fill(value: Any, where: dict[str, Path]) -> Any:
    if isinstance(value, list):
        return [_fill(v, where) for v in value]
    return value.format(**where) if isinstance(value, str) else value


@pytest.mark.parametrize(
    ("tool", "persona", "rule"),
    [
        ("mcp__qa-browser__browser_navigate", "qa-ui-function", None),
        ("mcp__qa-browser__browser_mouse_click_xy", "qa-regression", None),
        ("mcp__computer-use__screenshot", "qa-ui-function", None),
        ("mcp__claude-in-chrome__navigate", "qa-ui-function", "product-drivers"),
        ("mcp__qa-browser__browser_navigate", "qa-backend", "product-drivers"),
        ("mcp__computer-use__screenshot", "", "product-drivers"),
    ],
)
def test_only_ui_testers_drive_the_product_and_no_one_drives_the_owners_chrome(
    kit: Kit, tool: str, persona: str, rule: str | None
) -> None:
    caller = {"agent_id": TESTER_ID, "agent_type": persona} if persona else {}
    assert kit.rule(tool, {}, **caller) == rule


@pytest.mark.parametrize(
    ("earlier", "refused"),
    [({}, True), ({"started_as": "qa-ui-function-2"}, False), ({"state": "retired"}, False), ({"quiet": 400}, False),
     ({"parent": "another-session"}, False), ({"ago": 30}, False)],
    ids=["holds-it", "another-lane", "handed-back", "hand-back-lost", "another-session", "started-after"],
)  # fmt: skip
def test_a_lanes_browser_is_its_first_testers_while_that_one_runs(
    kit: Kit, earlier: dict[str, Any], refused: bool
) -> None:
    kit.session("qa-ui-function", sid=TESTER_ID, agent_id=TESTER_ID, parent=SID,
                started=(NOW - timedelta(seconds=60)).isoformat())  # fmt: skip
    other = "b7654321fedcba"
    kit.session("qa-ui-function", sid=other, agent_id=other, parent=earlier.get("parent", SID),
                state=earlier.get("state", "working"), started_as=earlier.get("started_as", "qa-ui-function"),
                started=(NOW - timedelta(seconds=earlier.get("ago", 120))).isoformat())  # fmt: skip
    wrote = (NOW - timedelta(seconds=earlier.get("quiet", 10))).timestamp()
    os.utime(kit.subagent(other, [entry(NOW, "user", "Scenarios and checks: 1")]), (wrote, wrote))

    decision = kit.decide("mcp__qa-browser__browser_navigate", {"url": "http://127.0.0.1:8101"}, **UI_TESTER)

    said = decision.reason if isinstance(decision, Refusal) else ""
    assert said.startswith("this browser is the qa-ui-function tester's started before you") is refused


def test_no_code_runs_in_the_browser_server(kit: Kit) -> None:
    assert kit.rule("mcp__qa-browser__browser_run_code_unsafe", {"code": "x"}, **UI_TESTER) == "qa-browser"


@pytest.mark.parametrize(
    ("asked", "refused"),
    [
        ({"pattern": "**/api/runs", "status": 502}, False),
        ({"pattern": "**/api/runs", "body": "{}"}, False),
        # Headers alone send the request on, past the route that keeps the dashboard out of the browser's reach.
        ({"pattern": "**", "removeHeaders": "origin"}, True),
        ({"pattern": "**/api/runs"}, True),
    ],
)
def test_a_route_answers_what_it_catches_and_never_sends_it_on(kit: Kit, asked: dict[str, Any], refused: bool) -> None:
    rule = kit.rule("mcp__qa-browser__browser_route", asked, **UI_TESTER)
    assert (rule == "qa-browser") is refused


@pytest.mark.parametrize(
    ("asked", "refused"),
    [
        ({"textGone": "Restarting", "time": 60}, True),
        ({"text": "Saved", "time": 5}, True),
        ({"textGone": "Restarting"}, False),
        ({"time": 5}, False),
    ],
)
def test_a_text_wait_gives_no_time_since_time_sleeps_before_it(kit: Kit, asked: dict[str, Any], refused: bool) -> None:
    rule = kit.rule("mcp__qa-browser__browser_wait_for", asked, **UI_TESTER)
    assert (rule == "qa-browser") is refused


def test_a_browser_call_that_names_a_file_is_refused_so_the_tester_reports_the_name_it_got(kit: Kit) -> None:
    asked = {"filename": "GOAL.md", "type": "png"}
    decision = kit.decide("mcp__qa-browser__browser_take_screenshot", asked, **UI_TESTER)
    assert isinstance(decision, Refusal) and decision.rule == "qa-browser" and "its answer says" in decision.reason


@pytest.mark.parametrize(
    ("tool", "asked", "field", "long", "short", "unit"),
    [("browser_wait_for", {}, "time", 3600, 3, 1), ("browser_wait_until", {"text": "Ready"}, "minutes", 60, 0.05, 60)],
)
def test_a_browser_wait_is_cut_to_the_testers_time_left(
    kit: Kit, tool: str, asked: dict[str, Any], field: str, long: float, short: float, unit: int
) -> None:
    decision = kit.decide("mcp__qa-browser__" + tool, {**asked, field: long}, **UI_TESTER)
    assert isinstance(decision, Rewrite) and 5 * 120 - 10 < decision.updated_input[field] * unit <= 60 + 5 * 120 - 10
    assert kit.decide("mcp__qa-browser__" + tool, {**asked, field: short}, **UI_TESTER) is None
