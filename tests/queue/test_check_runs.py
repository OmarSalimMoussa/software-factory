"""`factory check` through the warden: who runs the whole set and how often, and when a pass is recorded."""

from __future__ import annotations

import contextlib
import dataclasses

import pytest
from queue_kit import World

from runtime.guard import commit_check
from runtime.queue import formats, tasks
from runtime.queue.book import task_rel
from runtime.shared.models import Task
from runtime.shared.protocol import Refused
from runtime.warden import checks
from runtime.warden.sessions.launch import LaunchError

WORKTREE = "/home/agent/repo/.claude/worktrees/impl-a"
OTHER = "/home/agent/repo/.claude/worktrees/impl-b"
THIRD = "/home/agent/repo/.claude/worktrees/impl-c"


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
    world.launcher.git = _git(state)  # type: ignore[assignment]  # the fake launcher's git answers from `state`
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

    for whole in ({}, {"all": True}):  # --all on a branch records no pass, but it is a run of the whole set
        with pytest.raises(Refused, match="merge turn"):
            checks.check(ctx, whole, None)
    assert checks.check(ctx, {"only": ["tests"]}, None)["exit"] == 1  # named checks run any time, and are free

    tasks.set_status(world.ctx(), task.id, "turn", "", "")
    limit = world.settings.get_int("rounds.check_runs")
    for n in range(limit):
        checks.check(ctx, {"all": True} if n == 0 else {}, None)
    for whole in ({}, {"all": True}):
        with pytest.raises(Refused, match=f"{limit} check runs already"):
            checks.check(ctx, whole, None)
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

    with pytest.raises(Refused, match=r"commit your changes first: .* \(M app.py\)"):
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


def test_a_check_that_runs_tests_needs_a_place_in_the_factorys_memory_and_one_refused_costs_no_run(
    world: World, git: dict[str, str]
) -> None:
    _implementer(world, "turn")
    world.session("s-two", "implementer", "impl-b", worktree=OTHER)
    world.session("s-three", "implementer", "impl-c", task=world.task().id, worktree=THIRD)
    turn, two, off_turn = (world.ctx("implementer", s) for s in ("s-impl", "s-two", "s-three"))
    alongside: list[int] = []

    def run(checkout: str, argv: list[str]) -> tuple[int, str]:
        if checkout == OTHER:  # while impl-b's tests run
            with pytest.raises(Refused, match="impl-b is running tests"):
                checks.check(turn, {}, None)
            with pytest.raises(Refused, match="merge turn"):
                checks.check(off_turn, {}, None)
            alongside.append(checks.check(turn, {"only": ["size-ceilings"]}, None)["exit"])
        return 0, ""

    world.launcher.check = run
    checks.check(two, {"only": ["tests"]}, None)

    assert alongside == [0]
    assert world.launcher.checks == [(OTHER, ["--only", "tests"]), (WORKTREE, ["--only", "size-ceilings"])]
    limit = world.settings.get_int("rounds.check_runs")
    assert f"{limit - 1} whole runs left" in checks.check(turn, {}, None)["output"]


@pytest.mark.parametrize("ends", ["failed", "could-not-start"])
def test_a_checks_place_is_given_back_however_its_run_ends(world: World, git: dict[str, str], ends: str) -> None:
    world.session("s-impl", "implementer", "impl-a", worktree=WORKTREE)
    world.session("s-two", "implementer", "impl-b", worktree=OTHER)

    def run(checkout: str, argv: list[str]) -> tuple[int, str]:
        if ends == "could-not-start":
            raise LaunchError("software-check-run left no result")
        return 1, ""

    world.launcher.check = run
    with contextlib.suppress(LaunchError):
        checks.check(world.ctx("implementer", "s-impl"), {"only": ["tests"]}, None)
    world.launcher.check = lambda checkout, argv: (0, "")

    assert checks.check(world.ctx("implementer", "s-two"), {"only": ["tests"]}, None)["exit"] == 0


def test_a_ceiling_saved_since_the_install_adds_no_place_before_an_install_applies_it(
    world: World, git: dict[str, str]
) -> None:
    world.session("s-impl", "implementer", "impl-a", worktree=WORKTREE)
    world.session("s-two", "implementer", "impl-b", worktree=OTHER)
    world.with_settings(
        budget__test_memory_ceiling_mib=3584
    )  # two places, once an install gives the units that ceiling

    def run(checkout: str, argv: list[str]) -> tuple[int, str]:
        if checkout == WORKTREE:
            with pytest.raises(Refused, match="impl-a is running tests"):
                checks.check(world.ctx("implementer", "s-two"), {"only": ["tests"]}, None)
        return 0, ""

    world.launcher.check = run
    checks.check(world.ctx("implementer", "s-impl"), {"only": ["tests"]}, None)

    assert world.launcher.checks == [(WORKTREE, ["--only", "tests"])]


def test_a_budget_saved_with_no_room_for_the_installed_ceiling_refuses_every_test_run(
    world: World, git: dict[str, str]
) -> None:
    world.session("s-impl", "implementer", "impl-a", worktree=WORKTREE)
    world.with_settings(budget__slice_memory_high="10G", budget__test_memory_ceiling_mib=4096)  # one place, saved

    with pytest.raises(Refused, match="installed test ceiling, 6656 MiB"):
        checks.check(world.ctx("implementer", "s-impl"), {"only": ["tests"]}, None)
    assert world.launcher.checks == []
