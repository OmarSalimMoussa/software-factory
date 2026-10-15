"""Done: the tasks whose newest merge landed and stayed, how far each merge reached, the latest commits, and slot 0."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from loop_fakes import World, git, write

from runtime.queue import turn
from runtime.shared import store
from runtime.shared.protocol import Refused
from runtime.slots import health, leases
from runtime.warden.context import StackResult
from runtime.warden.delivery import deploys, done, github_push
from runtime.warden.health import probes

GITHUB = "git@github.com:owner/name.git"


def merge(world: World, task: str, slices: int) -> str:
    """The task's branch of ``slices`` commits merged onto master, and recorded as the warden records it."""
    checkout = world.checkout
    git(checkout, "checkout", "-q", "-b", task)
    for n in range(1, slices + 1):
        write(f"src/{task}-{n}.py")(checkout)
        git(checkout, "add", "-A")
        git(checkout, "commit", "-q", "-m", f"{task} part {n}\n\nSlice: {n}/{slices}")
    git(checkout, "checkout", "-q", "master")
    git(checkout, "merge", "-q", "--no-ff", "-m", f"Merge {task}", task)
    sha = git(checkout, "rev-parse", "HEAD")
    store.write_json(turn.merges_path(world.ctx.places), {**turn.merges(world.ctx.places), sha: task})
    return sha


class Slot0:
    """scripts/dev on slot 0, whose health prints the slot's address."""

    def __init__(self, port: int) -> None:
        self.port = port

    def run_stack(self, slot: int, verb: list[str], session: str, given: str = "") -> StackResult:
        return StackResult(0, f"http://127.0.0.1:{self.port}/healthz\n", "")


def listed(world: World) -> dict[str, Any]:
    found = done.read(world.ctx.places)
    assert found is not None
    return found


def test_a_task_is_done_while_its_newest_merge_stays_and_each_shows_how_far_it_reached(
    world: World, github_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = merge(world, "t-0001", 2)
    git(world.checkout, "push", "-q", str(github_repo), "master")
    taken_back = merge(world, "t-0002", 1)
    git(world.checkout, "revert", "-m", "1", "--no-edit", taken_back)
    merge(world, "t-0003", 1)
    with github_push.pushing:
        github_push.Git(world.ctx).run("fetch", "--quiet", GITHUB, "+refs/heads/master:refs/github/master")
    deploys.record(world.ctx.places, probes.STAGING, first[:7], world.clock.iso())
    world.ctx.launcher = Slot0(leases.port(world.ctx.settings(), 0))  # type: ignore[assignment]  # health alone
    monkeypatch.setattr(health, "http_get", lambda url, timeout, limit: json.dumps({"build_commit": first}).encode())

    done.refresh(world.ctx)

    found = listed(world)
    assert [(t["task"], t["github"], t["staging"], t["prod"]) for t in found["tasks"]] == [
        ("t-0003", False, False, None),
        ("t-0001", True, True, None),
    ]
    assert [c["slice"] for c in found["tasks"][1]["commits"]] == ["2/2", "1/2"]
    assert {(c["subject"], c["task"]) for c in found["commits"][:3]} == {
        ("t-0003 part 1", "t-0003"),
        ('Revert "Merge t-0002"', None),
        ("t-0002 part 1", "t-0002"),
    }
    after_the_push = 5  # t-0002's commit and merge, the revert, t-0003's commit and merge
    assert (found["ahead"], found["slot0"]) == (
        after_the_push,
        {"commit": first, "behind": after_the_push, "why": None},
    )


def test_what_the_warden_has_not_seen_stays_unknown(world: World, github_repo: Path) -> None:
    merge(world, "t-0001", 1)

    done.refresh(world.ctx)

    found = listed(world)
    row = found["tasks"][0]
    assert (found["github"], found["ahead"], row["github"], row["staging"], row["prod"]) == (None,) * 5
    assert found["slot0"]["commit"] is None and "bring the slot up first" in found["slot0"]["why"]


def test_the_list_is_read_again_once_its_period_has_passed(world: World, github_repo: Path) -> None:
    merge(world, "t-0001", 1)
    done.refresh(world.ctx)
    merge(world, "t-0002", 1)
    world.clock.t += 60
    done.refresh(world.ctx)
    assert [t["task"] for t in listed(world)["tasks"]] == ["t-0001"]

    world.clock.t += world.ctx.settings().get_int("watch.done_minutes") * 60
    done.refresh(world.ctx)

    assert [t["task"] for t in listed(world)["tasks"]] == ["t-0002", "t-0001"]


def test_a_revert_of_a_revert_brings_the_task_back(world: World, github_repo: Path) -> None:
    first = merge(world, "t-0001", 1)
    git(world.checkout, "revert", "-m", "1", "--no-edit", first)
    git(world.checkout, "revert", "--no-edit", "HEAD")

    done.refresh(world.ctx)

    assert [t["task"] for t in listed(world)["tasks"]] == ["t-0001"]


def test_a_read_that_failed_waits_its_period_before_it_is_tried_again(world: World) -> None:
    with pytest.raises(Refused):  # no deploy key, so no mirror: a factory not installed yet
        done.refresh(world.ctx)
    done.refresh(world.ctx)
    world.clock.t += world.ctx.settings().get_int("watch.done_minutes") * 60

    with pytest.raises(Refused):
        done.refresh(world.ctx)
