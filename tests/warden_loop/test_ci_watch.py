"""CI's run of the warden's last push: followed until it ends, and a failure filed once, naming its jobs."""

from __future__ import annotations

from typing import Any

from loop_fakes import World

from runtime.queue.book import Queue
from runtime.warden import ci_watch, deploys, probes


def _run(status: str, conclusion: str | None, failed: str = "") -> dict[str, Any]:
    jobs = {"checks": {"status": status, "conclusion": "success"}, "smoke": {"status": status, "conclusion": None}}
    if failed:
        jobs["smoke"] = {"status": "completed", "conclusion": "failure"}
    return {"id": "ci-7", "status": status, "conclusion": conclusion, "url": "https://github.test/runs/7", "jobs": jobs}


def test_a_failed_ci_run_of_the_last_push_is_filed_once_and_kept_for_home(world: World) -> None:
    deploys.expect(world.ctx.places, probes.STAGING, "d" * 40, world.clock.now(), 30)
    period = world.ctx.settings().get_int("watch.ci_poll_minutes") * 60

    ci_watch.poll(world.ctx)  # GitHub has not started a run yet
    world.github.ci = _run("in_progress", None)
    world.clock.t += period
    ci_watch.poll(world.ctx)
    assert ci_watch.read(world.ctx.places)["status"] == "in_progress"

    world.github.ci = _run("completed", "failure", failed="smoke")
    for _ in range(2):
        world.clock.t += period
        ci_watch.poll(world.ctx)
    found = ci_watch.read(world.ctx.places)
    requests = Queue.of(world.ctx).requests()[0]
    assert (found["failed"], len(requests)) == (["smoke"], 1)
    assert "smoke failed" in requests[0].body and requests[0].id == found["request"]


def test_a_passing_run_files_nothing(world: World) -> None:
    deploys.expect(world.ctx.places, probes.STAGING, "d" * 40, world.clock.now(), 30)
    world.github.ci = _run("completed", "success")
    ci_watch.poll(world.ctx)
    assert ci_watch.read(world.ctx.places)["conclusion"] == "success"
    assert Queue.of(world.ctx).requests()[0] == []
