"""CI's run of the warden's last push: followed until it ends, and a failure filed once, naming its jobs, the steps
that failed and each one's log, which agents cannot open on GitHub."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from loop_fakes import World

from runtime.queue import intake
from runtime.queue.book import Queue
from runtime.warden.delivery import ci_watch, deploys
from runtime.warden.health import probes


def _run(status: str, conclusion: str | None, failed: str = "") -> dict[str, Any]:
    jobs = {"checks": {"id": "71", "status": status, "conclusion": "success", "steps": []},
            "smoke": {"id": "72", "status": status, "conclusion": None, "steps": []}}  # fmt: skip
    if failed:
        steps = [{"name": "Set up job", "conclusion": "success"},
                 {"name": "Run the smoke test", "conclusion": "failure"}]  # fmt: skip
        jobs["smoke"] = {"id": "72", "status": "completed", "conclusion": "failure", "steps": steps}
    return {"id": "ci-7", "status": status, "conclusion": conclusion, "url": "https://github.test/runs/7", "jobs": jobs}


def _failed(world: World, log: str | None, job_id: str | None = "72") -> str:
    """The body of the request a failed run of the last push files, its smoke job's log ``log`` (None: unreadable)."""
    if log is not None:
        world.github.logs["72"] = log
    deploys.expect(world.ctx.places, probes.STAGING, "d" * 40, world.clock.now(), 30)
    world.github.ci = _run("completed", "failure", failed="smoke")
    world.github.ci["jobs"]["smoke"]["id"] = job_id
    ci_watch.poll(world.ctx)
    [request] = Queue.of(world.ctx).requests()[0]
    return request.body


def _logged(*lines: str) -> str:
    return "\ufeff" + "".join(f"2026-10-16T08:23:42.{n:07d}Z {line}\n" for n, line in enumerate(lines))


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


def test_a_failed_jobs_request_names_the_step_and_quotes_its_log_up_to_its_last_error(world: World) -> None:
    log = _logged(
        "Run the lint, which may fail",
        "##[error]lint found 2 warnings",
        "Run the smoke test",
        "\x1b[31mFAIL  deploy-shape\x1b[0m",
        "      add the line **/.claude",
        "##[error]Process completed with exit code 1.",
        "Post job cleanup.",
        "Cleaning up orphan processes",
    )

    body = _failed(world, log)

    assert 'smoke failed at "Run the smoke test". Its log, up to its last error:' in body
    assert "lint found 2 warnings\n    Run the smoke test\n    FAIL  deploy-shape\n          add the line" in body
    assert "add the line **/.claude\n    ##[error]Process completed with exit code 1." in body
    assert "Post job cleanup" not in body and "2026-10-16T" not in body and "\x1b" not in body


def test_a_long_log_is_quoted_from_a_whole_line_and_within_its_budget(world: World) -> None:
    budget = world.ctx.settings().get_int("watch.ci_log_kib") * 1024
    lines = [f"line {n:05d} {'x' * 60}" for n in range(2 * budget // 60)]

    body = _failed(world, _logged(*lines, "##[error]Process completed with exit code 1."))

    quoted = body.split("up to its last error:\n\n", 1)[1]
    assert len(quoted.encode()) <= budget + 4 * quoted.count("\n") + 4
    assert quoted.startswith("    line ") and quoted.endswith("##[error]Process completed with exit code 1.")


@pytest.mark.parametrize(
    ("log", "job_id", "why"),
    [(None, "72", "GitHub refused GET"), ("##[error]never read", None, "GitHub named no id for the job")],
    ids=["refused", "no-id"],
)
def test_a_log_that_cannot_be_read_is_reported_and_the_request_says_why(
    world: World, caplog: pytest.LogCaptureFixture, log: str | None, job_id: str | None, why: str
) -> None:
    with caplog.at_level(logging.WARNING):
        body = _failed(world, log, job_id)

    assert f'smoke failed at "Run the smoke test". Its log could not be read: {why}' in body
    assert "None" not in body and "never read" not in body
    warned = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    assert any(f"CI job smoke (id {job_id})" in said for said in warned)


def test_a_request_that_cannot_be_filed_leaves_the_run_open_and_is_filed_at_the_next_poll(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    deploys.expect(world.ctx.places, probes.STAGING, "d" * 40, world.clock.now(), 30)
    world.github.ci = _run("completed", "failure", failed="smoke")
    filing = intake.file_request

    def disk_full(*args: object) -> None:
        monkeypatch.setattr(intake, "file_request", filing)
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(intake, "file_request", disk_full)
    ci_watch.poll(world.ctx)
    assert (ci_watch.read(world.ctx.places)["status"], Queue.of(world.ctx).requests()[0]) == (None, [])

    world.clock.t += world.ctx.settings().get_int("watch.ci_poll_minutes") * 60
    ci_watch.poll(world.ctx)
    found = ci_watch.read(world.ctx.places)
    [request] = Queue.of(world.ctx).requests()[0]
    assert (found["status"], found["request"]) == ("completed", request.id)
