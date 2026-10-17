"""What the Done refresh does with the default branch: GitHub's follows the checkout's, a fast-forward of whatever it
holds, and the orchestrator is asked to check a tip that waits on nothing else."""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from loop_fakes import World, commit, git, passed, write

from runtime.queue import turn
from runtime.warden import inbox
from runtime.warden.delivery import deploys, done, github_push
from runtime.warden.health import probes


def refresh(world: World) -> dict[str, object]:
    """The Done refresh, once watch.done_minutes have passed as its pass waits; the list it wrote."""
    world.clock.t += world.ctx.settings().get_int("watch.done_minutes") * 60
    done.refresh(world.ctx)
    found = done.read(world.ctx.places)
    assert found is not None
    return found


@pytest.mark.parametrize(
    "change",
    [write("src/app.py"), write(".github/workflows/deploy.yml"), write("factory.toml")],
    ids=["code", "workflow", "settings"],
)
def test_github_follows_the_checkouts_branch_whatever_the_tip_changes(
    world: World, github_repo: Path, change: Callable[[Path], None]
) -> None:
    commit(world.checkout, write("src/first.py"))
    tip = commit(world.checkout, change)  # never checked, and touching what only the owner changes

    found = refresh(world)

    assert git(github_repo, "rev-parse", "master") == tip
    assert (found["github"], found["ahead"]) == (tip, 0)


def test_the_mirror_reads_the_agents_checkout_though_another_user_owns_it(
    world: World, github_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tip = commit(world.checkout, write("src/app.py"))
    # Git's own test switch makes every repository another user's; the global config trusts the warden's mirror and
    # GitHub's stand-in, never the agent's checkout.
    config = github_repo.parent / "gitconfig"
    mirror = world.ctx.places.mirror
    config.write_text(config.read_text() + f"[safe]\n\tdirectory = {mirror}\n\tdirectory = {github_repo}\n")
    monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")

    refresh(world)

    assert git(github_repo, "rev-parse", "master") == tip


@pytest.mark.parametrize("trouble", ["github-moved", "github-gone", "github-refuses"])
def test_a_branch_github_moved_on_its_own_refused_or_not_answering_is_left_and_logged_once_with_githubs_reason(
    world: World, github_repo: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture, trouble: str
) -> None:
    held = git(github_repo, "rev-parse", "master")
    if trouble == "github-moved":
        elsewhere = tmp_path / "elsewhere"
        git(tmp_path, "clone", "-q", str(github_repo), str(elsewhere))
        held = commit(elsewhere, write("src/other.py"))
        git(elsewhere, "push", "-q", "origin", "master")
    elif trouble == "github-refuses":
        hook = github_repo / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho 'error: GH013: Repository rule violations found' >&2\nexit 1\n")
        hook.chmod(0o755)
    else:
        shutil.rmtree(github_repo)
    tip = commit(world.checkout, write("src/app.py"))

    with caplog.at_level(logging.WARNING, logger=github_push.__name__):
        refresh(world)
        found = refresh(world)

    said = {
        "github-moved": "has commits the checkout lacks",
        "github-gone": "git fetch failed",
        "github-refuses": "git push failed: remote: error: GH013: Repository rule violations found",
    }[trouble]
    assert [r.levelname for r in caplog.records] == ["WARNING"] and said in caplog.records[0].getMessage()
    assert found["local"] == tip  # the list is written all the same
    if trouble != "github-gone":
        assert git(github_repo, "rev-parse", "master") == held
    if trouble == "github-moved":
        assert found["github"] == held


@pytest.mark.parametrize(
    ("why", "asked"),
    [("only-its-check", True), ("a-task-holds-the-turn", False), ("an-unchecked-merge", False),
     ("nothing-since-prod", False), ("its-check-passed", False)],
)  # fmt: skip
def test_the_orchestrator_is_asked_once_to_check_a_tip_that_waits_on_nothing_else(
    world: World, github_repo: Path, monkeypatch: pytest.MonkeyPatch, why: str, asked: bool
) -> None:
    deploys.record(world.ctx.places, probes.PROD, git(world.checkout, "rev-parse", "HEAD")[:8], world.clock.iso())
    if why != "nothing-since-prod":
        commit(world.checkout, write("src/app.py"))  # such as a revert, or the owner's own change
    if why == "a-task-holds-the-turn":
        monkeypatch.setattr(turn, "holder", lambda places: {"task": "t-0001", "branch": "b", "base": "c"})
    elif why == "an-unchecked-merge":
        git(world.checkout, "checkout", "-q", "-b", "side")
        commit(world.checkout, write("src/side.py"))
        git(world.checkout, "checkout", "-q", "-")
        git(world.checkout, "merge", "-q", "--no-ff", "-m", "Merge side", "side")
    elif why == "its-check-passed":
        passed(world, git(world.checkout, "rev-parse", "HEAD^{tree}"))
    tip = git(world.checkout, "rev-parse", "HEAD")

    refresh(world)
    refresh(world)

    assert [e for e in world.events if e[0] == inbox.CHECK_DUE] == ([(inbox.CHECK_DUE, {"commit": tip})] * asked)
