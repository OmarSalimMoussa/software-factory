"""queue-format: every rule of the intake spec on one file and across files, and the sweep's one request per
failing file and content (R30)."""

from __future__ import annotations

import dataclasses

import pytest
from queue_kit import World

from runtime.queue import format_check, formats, records
from runtime.queue.book import request_rel, task_rel
from runtime.queue.formats import FormatError
from runtime.shared.models import Request, Task

REQ = Request("r-2026-10-09-0001", "agents", "implementer", "2026-10-09T12:00:00Z", "normal", "the sign-in page",
              "It loops.", "", "task t-0001")  # fmt: skip
TASK = Task("t-0001", "Sign-in loops", ["r-2026-10-09-0001"], "G1", "major", ["ui"], "queued",
            "2026-10-09T12:00:00Z", "Fix it.", cause="web/auth.py:41", slices=1)  # fmt: skip


def test_a_task_and_a_request_round_trip_through_their_text() -> None:
    task = dataclasses.replace(TASK, score=72.5, run=1, why="first visit", picture="pictures/t-0001-ab.svg",
                               status="proposed", after=["t-0002"], not_now=True)  # fmt: skip
    assert formats.parse_task(formats.dump_task(task), "tasks/t-0001.md") == task
    assert formats.parse_request(formats.dump_request(REQ), request_rel(REQ)) == REQ


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"inbox": "users", "from_role": "user-7", "report": "7c1e", "severity": "severe"}, "never severe"),
        ({"outcome": "merged t-0001"}, "outcome"),
        ({"where": ""}, "where is empty"),
        ({"inbox": "owner"}, "cannot file in the owner inbox"),
        ({"report": "7c1e"}, "names its report"),
    ],
)
def test_a_request_breaking_a_rule_is_malformed(change: dict[str, str], problem: str) -> None:
    req = dataclasses.replace(REQ, **change)
    with pytest.raises(FormatError, match=problem):
        formats.parse_request(formats.dump_request(req))


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"goal_item": ""}, "names a goal item"),
        ({"requests": []}, "at least one request"),
        ({"status": "proposed", "run": 1}, "why and its picture"),
        ({"status": "building", "slices": None, "run": 1}, "surfaces, slices and run"),
        ({"surfaces": ["none", "ui"]}, "none beside"),
        ({"owner_class": "huge"}, "owner_class"),
    ],
)
def test_a_task_breaking_a_rule_is_malformed(change: dict[str, object], problem: str) -> None:
    with pytest.raises(FormatError, match=problem):
        formats.parse_task(formats.dump_task(dataclasses.replace(TASK, **change)))


def test_a_missing_or_unknown_key_is_never_filled_in() -> None:
    text = formats.dump_task(TASK)
    with pytest.raises(FormatError, match="missing branch"):
        formats.parse_task(text.replace("branch:\n", ""))
    with pytest.raises(FormatError, match="unknown colour"):
        formats.parse_task(text.replace("---\n#", "colour: red\n---\n#"))


def test_a_value_cannot_smuggle_a_second_line_into_the_front_matter() -> None:
    with pytest.raises(FormatError, match="one line"):
        formats.dump_task(dataclasses.replace(TASK, branch="b\nstatus: approved"))


def test_files_must_point_at_each_other() -> None:
    blobs = {
        request_rel(REQ): formats.dump_request(dataclasses.replace(REQ, outcome="task t-0009")).encode(),
        task_rel(TASK.id): formats.dump_task(TASK).encode(),
    }
    problems = format_check.check(blobs)
    assert any("does not name t-0001" in p for p in problems[task_rel(TASK.id)])
    assert any("names t-0009, which does not link it" in p for p in problems[request_rel(REQ)])


def test_a_check_record_keeps_the_table_the_skills_write() -> None:
    table = f"{records.CHECK_TABLE}\n| --- | --- | --- | --- |\n| sign-in | pass | 4 | video |"
    text = f"---\nid: c-1\ncommit: abc1234\nat: 2026-10-09T12:00:00Z\n---\n# Check\n\n## Scenarios\n\n{table}\n"
    assert records.parse_check(text).rows[0].steps == 4
    with pytest.raises(FormatError, match="steps"):
        records.parse_check(text.replace("| 4 |", "| four |"))


def test_a_write_that_fails_queue_format_is_refused_whole(world: World) -> None:
    task = world.task()
    broken = dataclasses.replace(task, goal_item="")
    with pytest.raises(Exception, match="queue-format"):
        world.queue().write_task(broken)
    assert world.queue().task(task.id).goal_item == "G1"


def test_the_sweep_files_one_request_per_failing_file_and_content(world: World) -> None:
    world.write_raw("tasks/t-0042.md", "not a task")
    first = format_check.sweep(world.ctx())
    assert len(first) == 1 and format_check.sweep(world.ctx()) == []
    world.write_raw("tasks/t-0042.md", "still not a task")
    assert len(format_check.sweep(world.ctx())) == 1
    filed = world.queue().request(first[0])
    assert (filed.inbox, filed.from_role, filed.where) == ("agents", "warden", "queue/tasks/t-0042.md")
