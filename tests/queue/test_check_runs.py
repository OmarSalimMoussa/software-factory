"""`factory check` through the warden: who runs the whole set and how often, and when a pass is recorded."""

from __future__ import annotations

import dataclasses

import pytest
from queue_kit import World

from runtime.guard import commit_check
from runtime.queue import formats, tasks
from runtime.queue.book import task_rel
from runtime.shared.models import Task
from runtime.shared.protocol import Refused
from runtime.warden import checks

WORKTREE = "/home/agent/repo/.claude/worktrees/impl-a"


def _git(state: dict[str, str]) -> object:
    """git in the checkout as ``state`` says: its commit, tree, branch, uncommitted changes and first-parent log."""

    def answer(argv: list[str]) -> tuple[int, str]:
        words = argv[3:]
        if words[0] == "status":
            return 0, state["dirty"]
        if words[0] == "rev-parse":
            return 0, f"{state['commit']}\n{state['tree']}\n{state['branch']}\n"
        if words[0] == "log":
            return 0, state["log"]
        raise AssertionError(argv)

    return answer


@pytest.fixture
def git(world: World) -> dict[str, str]:
    state = {"commit": "c1" * 20, "tree": "t1" * 20, "branch": "impl-a", "dirty": "", "log": ""}
    world.launcher.git = _git(state)  # type: ignore[assignment]
    return state


def _implementer(world: World, status: str) -> Task:
    task = world.task()
    world.write_raw(task_rel(task.id), formats.dump_task(dataclasses.replace(task, status=status, run=1)))
    world.session("s-impl", "implementer", "impl-a", task=task.id, worktree=WORKTREE)
    return task


def test_an_implementer_runs_the_whole_set_only_on_its_merge_turn_and_at_most_its_limit(
    world: World, git: dict[str, str]
) -> None:
    task = _implementer(world, "qa-clean")
    ctx = world.ctx("implementer", "s-impl")
    world.launcher.check = lambda checkout, args: (1, "factory check: 1 of 29 failed\n")

    with pytest.raises(Refused, match="merge turn"):
        checks.check(ctx, {}, None)
    assert checks.check(ctx, {"only": ["tests"]}, None)["exit"] == 1  # named checks run any time, and are free

    tasks.set_status(world.ctx(), task.id, "turn", "", "")
    limit = world.settings.get_int("rounds.check_runs")
    for _ in range(limit):
        checks.check(ctx, {}, None)
    with pytest.raises(Refused, match=f"{limit} check runs already"):
        checks.check(ctx, {}, None)
    assert len(world.launcher.checks) == limit + 1

    for to, event, by in [("merged", "merged", "warden"), ("building", "reverted", "warden"),
                          ("qa", "qa-ready", "implementer"), ("qa-clean", "qa-clean", "implementer")]:  # fmt: skip
        tasks.move(world.ctx(), task.id, to, event, by)
    tasks.set_status(world.ctx(), task.id, "turn", "", "")
    assert "whole runs left" in checks.check(ctx, {}, None)["output"]  # a new turn counts from none


def test_a_whole_run_that_passed_on_an_unmoved_tree_is_recorded_for_the_push_and_the_merge_hook(
    world: World, git: dict[str, str]
) -> None:
    _implementer(world, "turn")
    ctx = world.ctx("implementer", "s-impl")

    world.launcher.check = lambda checkout, args: (0, "factory check: 29 passed\n")
    answer = checks.check(ctx, {}, None)

    assert answer["passed"] and world.launcher.checks == [(WORKTREE, [])]
    assert checks.passed(world.places, git["tree"]) and commit_check.passed(world.places, git["tree"])
    again = checks.check(ctx, {}, None)
    assert again["passed"] and "already passed" in again["output"] and len(world.launcher.checks) == 1


@pytest.mark.parametrize(
    ("args", "code", "moved"),
    [({}, 1, False), ({"only": ["tests"]}, 0, False), ({}, 0, True), ({"all": True}, 0, False)],
    ids=["failed", "part-of-the-set", "checkout-moved", "all-on-a-branch-runs-no-ratchet"],
)
def test_no_pass_is_recorded_for_a_failure_part_of_the_set_or_a_checkout_that_moved(
    world: World, git: dict[str, str], args: dict[str, object], code: int, moved: bool
) -> None:
    _implementer(world, "turn")

    def run(checkout: str, argv: list[str]) -> tuple[int, str]:
        if moved:
            git["tree"] = "t2" * 20
        return code, ""

    world.launcher.check = run
    assert not checks.check(world.ctx("implementer", "s-impl"), args, None)["passed"]
    assert not checks.passed(world.places, "t1" * 20) and not checks.passed(world.places, "t2" * 20)


def test_a_checkout_with_uncommitted_changes_is_refused_before_anything_runs(world: World, git: dict[str, str]) -> None:
    _implementer(world, "turn")
    git["dirty"] = " M app.py\n"

    with pytest.raises(Refused, match="commit your changes first"):
        checks.check(world.ctx("implementer", "s-impl"), {}, None)
    assert world.launcher.checks == []


@pytest.mark.parametrize(
    ("passed", "args"),
    [(["t0" * 20], ["--since", "c0" * 20]), ([], ["--all"])],
    ids=["from-the-newest-passed-commit", "nothing-passed-yet"],
)
def test_on_the_default_branch_a_check_covers_what_changed_since_the_newest_commit_that_passed(
    world: World, git: dict[str, str], passed: list[str], args: list[str]
) -> None:
    world.session("s-orch", "orchestrator", "orchestrator", worktree=str(world.repo))
    git["branch"] = world.settings.get_str("repo.default_branch")
    git["log"] = f"{'c1' * 20} {'t1' * 20}\n{'c0' * 20} {'t0' * 20}\n{'cz' * 20} {'tz' * 20}\n"
    ctx = world.ctx()
    for tree in passed:
        world.launcher.check = lambda checkout, argv: (0, "")
        git["tree"] = tree
        checks.check(ctx, {}, None)
    git["tree"] = "t1" * 20
    world.launcher.checks.clear()

    checks.check(ctx, {}, None)
    assert world.launcher.checks == [(str(world.repo), args)]
