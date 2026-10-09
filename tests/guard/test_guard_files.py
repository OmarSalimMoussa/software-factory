"""What a session may write and ask: edits stay in its own worktree, protected paths and the queue are never written
by hand, a task's status is the orchestrator's, and the factory command runs alone."""

from __future__ import annotations

from pathlib import Path

import pytest
from guard_kit import Kit

from runtime.guard.facts import Rewrite


@pytest.fixture
def kit(tmp_path: Path) -> Kit:
    made = Kit(tmp_path)
    made.session("implementer")
    return made


@pytest.mark.parametrize(
    ("path", "rule"),
    [
        ("{worktree}/src/app.py", None),
        ("{repo}/.worktrees/impl-b/src/app.py", "own-worktree"),
        ("{repo}/src/app.py", "own-worktree"),
        ("{home}/notes.md", "own-worktree"),
        ("{worktree}/GOAL.md", "protected-paths"),
        ("{worktree}/goal/hero.png", "protected-paths"),
        ("{worktree}/.github/workflows/ci.yml", "protected-paths"),
        ("{worktree}/scripts/ci/promote", "protected-paths"),
        ("{worktree}/check-values/surfaces.toml", "protected-paths"),
        ("{worktree}/.git", "protected-paths"),
        ("{repo}/queue/tasks/t-0001.md", "queue-status"),
        ("{install}/runtime/guard/hook.py", "protected-paths"),
        ("{home}/.claude/settings.json", "protected-paths"),
    ],
)
def test_an_edit_stays_in_the_sessions_own_worktree_and_off_protected_paths(
    kit: Kit, path: str, rule: str | None
) -> None:
    target = path.format(worktree=kit.worktree, repo=kit.repo, home=kit.home, install=kit.places.install)
    assert kit.rule("Write", {"file_path": target, "content": "x"}) == rule


def test_an_edit_through_a_link_out_of_the_worktree_is_refused(kit: Kit) -> None:
    (kit.worktree / "out").symlink_to(kit.home)
    assert kit.rule("Edit", {"file_path": str(kit.worktree / "out" / "x.py")}) == "own-worktree"


def test_the_orchestrators_worktree_is_the_main_checkout(tmp_path: Path) -> None:
    kit = Kit(tmp_path)
    kit.session("orchestrator", worktree=kit.repo)
    assert kit.rule("Write", {"file_path": str(kit.repo / "notes.md"), "content": "x"}) is None
    assert kit.rule("Write", {"file_path": str(kit.repo / "GOAL.md"), "content": "x"}) == "protected-paths"
    kit.session("implementer", sid="impl-1")
    assert kit.rule("Write", {"file_path": str(kit.worktree / "app.py"), "content": "x"}) == "own-worktree"


@pytest.mark.parametrize(
    ("command", "rule"),
    [
        ("echo x > {repo}/queue/tasks/t-0001.md", "queue-status"),
        ("cd {repo} && rm -rf queue", "queue-status"),
        ("factory task status t-0001 building", "queue-status"),
        ("factory queue task new --title x", "factory-verbs"),
        ("factory promote", "factory-verbs"),
        ("factory snapshot", "factory-verbs"),
        ("factory push abc1234", "factory-verbs"),
        ("factory no-such-verb", "factory-verbs"),
        ("factory status && touch x", "factory-alone"),
        ("factory status | tail -3", "factory-alone"),
        ("timeout 60 factory status", "factory-alone"),
        ("factory status > {home}/.bashrc", "factory-alone"),
        ("FACTORY_X=1 factory status", "factory-alone"),
        ("factory request new --from implementer < {home}/.claude/.credentials.json", "factory-alone"),
        ("factory request new --from implementer --where {home}/.claude/.credentials.json", "factory-alone"),
        ("factory request new --from implementer --severity normal --where x < {worktree}/body.md", None),
        ("factory status", None),
        ("factory qa plan", None),
        ("factory slot up web", None),
        ("factory request new --from implementer --severity normal --where /api/users <<'EOF'\nbody\nEOF", None),
        ("factory spec save t-0001 {worktree}/spec.md", "factory-verbs"),
    ],
)
def test_the_queue_and_the_factory_command_are_used_only_as_the_role_allows(
    kit: Kit, command: str, rule: str | None
) -> None:
    (kit.home / ".claude").mkdir()
    (kit.home / ".claude" / ".credentials.json").write_text("{}")
    assert kit.bash(command.format(repo=kit.repo, home=kit.home, worktree=kit.worktree)) == rule


@pytest.mark.parametrize(
    ("role", "command", "subagent", "rule"),
    [
        ("orchestrator", "factory task status t-0001 building", False, None),
        ("orchestrator", "factory task status t-0001 building", True, "queue-status"),
        ("orchestrator", "factory queue task new --title x", True, None),
        ("orchestrator", "factory inbox --wait", True, "factory-verbs"),
        ("orchestrator", "factory start implementer t-0001", False, None),
        ("orchestrator", "factory start owner mine", False, "factory-verbs"),
        ("orchestrator", "factory start orchestrator", False, "factory-verbs"),
        ("designer", "factory spec save t-0001 spec.md", False, None),
        ("designer", "factory qa-ready abc1234", False, "factory-verbs"),
        ("implementer", "factory request new --from qa-backend", True, None),
        ("implementer", "factory check", True, "factory-verbs"),
    ],
)
def test_each_role_and_its_subagents_run_only_their_verbs(
    tmp_path: Path, role: str, command: str, subagent: bool, rule: str | None
) -> None:
    kit = Kit(tmp_path)
    kit.session(role, worktree=kit.repo if role == "orchestrator" else kit.worktree)
    caller = {"agent_id": "a1234567890abc", "agent_type": "intake"} if subagent else {}
    assert kit.bash(command, cwd=str(kit.repo if role == "orchestrator" else kit.worktree), **caller) == rule


def test_a_subagents_factory_call_carries_its_id(tmp_path: Path) -> None:
    kit = Kit(tmp_path)
    kit.session("orchestrator", worktree=kit.repo)
    intake = {"agent_id": "a1234567890abc", "agent_type": "intake", "cwd": str(kit.repo)}
    decision = kit.decide("Bash", {"command": "factory queue task new --title x"}, **intake)
    assert isinstance(decision, Rewrite)
    stamped = "export SOFTWARE_AGENT_ID=a1234567890abc; factory queue task new --title x"
    assert decision.updated_input["command"] == stamped
    assert kit.decide("Bash", {"command": "factory status"}, cwd=str(kit.repo)) is None


@pytest.mark.parametrize("caller", [{}, {"agent_id": "a1234567890abc", "agent_type": "intake"}], ids=["it", "intake"])
@pytest.mark.parametrize("command", ["SOFTWARE_AGENT_ID=a1 factory status", "echo $SOFTWARE_AGENT_ID"])
def test_no_command_names_the_subagent_id_variable(tmp_path: Path, caller: dict[str, str], command: str) -> None:
    kit = Kit(tmp_path)
    kit.session("orchestrator", worktree=kit.repo)
    assert kit.bash(command, cwd=str(kit.repo), **caller) == "agent-id"
