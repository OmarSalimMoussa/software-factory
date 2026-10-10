"""Errors and traces are read by the sessions and, among subagents, by intake, triage and the testers only."""

from __future__ import annotations

from pathlib import Path

import pytest
from guard_kit import NOW, TESTER_ID, Kit, entry


@pytest.mark.parametrize(
    ("role", "kind", "rule"),
    [
        ("orchestrator", "", None),
        ("implementer", "", None),
        ("designer", "", None),
        ("orchestrator", "intake", None),
        ("orchestrator", "triage", None),
        ("orchestrator", "qa-prod", None),
        ("implementer", "qa-backend", None),
        ("designer", "critic-security", "factory-verbs"),
        ("implementer", "Explore", "factory-verbs"),
    ],
)
def test_only_sessions_intake_triage_and_testers_read_errors_and_traces(
    tmp_path: Path, role: str, kind: str, rule: str | None
) -> None:
    kit = Kit(tmp_path)
    where = kit.repo if role == "orchestrator" else kit.worktree
    kit.session(role, worktree=where)
    kit.subagent(TESTER_ID, [entry(NOW, "user", "Scenarios and checks: 1")])
    caller = {"agent_id": TESTER_ID, "agent_type": kind} if kind else {}
    for command in ("factory errors --where slot 1 --issue 12", "factory traces --trace abc"):
        assert kit.bash(command, cwd=str(where), **caller) == rule, command
