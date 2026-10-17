"""Done: the tasks whose newest merge landed and stayed, how far each merge reached, the latest commits, and slot 0."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from loop_fakes import World, git, write

from runtime.queue import turn
from runtime.shared import models, store
from runtime.shared.models import Probe
from runtime.shared.protocol import Refused
from runtime.warden.delivery import deploys, done
from runtime.warden.health import probes


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


def staging(world: World, commit: str, error: str = "") -> None:
    """Staging's probe, which is slot 0's: its build, or why it gave none."""
    store.append_jsonl(world.ctx.places.probes, models.dump(Probe(world.clock.iso(), "staging", not error, commit, 40,
                                                                  error)))  # fmt: skip


def listed(world: World) -> dict[str, Any]:
    found = done.read(world.ctx.places)
    assert found is not None
    return found


def test_a_task_is_done_while_its_newest_merge_stays_and_each_shows_how_far_it_reached(
    world: World, github_repo: Path
) -> None:
    first = merge(world, "t-0001", 2)
    taken_back = merge(world, "t-0002", 1)
    git(world.checkout, "revert", "-m", "1", "--no-edit", taken_back)
    merge(world, "t-0003", 1)
    deploys.record(world.ctx.places, probes.STAGING, first[:7], world.clock.iso())
    staging(world, first[:7])

    done.refresh(world.ctx)

    found = listed(world)
    assert [(t["task"], t["github"], t["staging"], t["prod"]) for t in found["tasks"]] == [
        ("t-0003", True, False, None),
        ("t-0001", True, True, None),
    ]
    assert [c["slice"] for c in found["tasks"][1]["commits"]] == ["2/2", "1/2"]
    assert {(c["subject"], c["task"]) for c in found["commits"][:3]} == {
        ("t-0003 part 1", "t-0003"),
        ('Revert "Merge t-0002"', None),
        ("t-0002 part 1", "t-0002"),
    }
    after_slot0 = 5  # t-0002's commit and merge, the revert, t-0003's commit and merge
    assert (found["ahead"], found["slot0"]) == (0, {"commit": first[:7], "behind": after_slot0, "why": None})


@pytest.mark.parametrize("error", ["", "HTTP 502"], ids=["never-probed", "red"])
def test_what_the_warden_has_not_seen_stays_unknown(world: World, github_repo: Path, error: str) -> None:
    merge(world, "t-0001", 1)
    shutil.rmtree(github_repo)  # GitHub never answered
    if error:
        staging(world, "", error)

    done.refresh(world.ctx)

    found = listed(world)
    row = found["tasks"][0]
    assert (found["github"], found["ahead"], row["github"], row["staging"], row["prod"]) == (None,) * 5
    assert found["slot0"] == {"commit": None, "behind": None, "why": error or "staging has not been probed yet"}


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
