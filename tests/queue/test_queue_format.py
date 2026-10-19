"""queue-format: every rule of the intake spec on one file and across files, and the sweep's one request per
failing file and content (R30)."""

from __future__ import annotations

import dataclasses
import logging

import pytest
from queue_kit import World

from runtime.queue import format_check, formats, records
from runtime.queue.book import request_rel, task_rel
from runtime.queue.files import QueueFiles
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
        ({"inbox": "chat", "from_role": "discord-6381", "report": "9", "severity": "severe"}, "never severe"),
        ({"inbox": "chat", "from_role": "implementer", "report": "9"}, "cannot file in the chat inbox"),
        ({"inbox": "chat", "from_role": "discord-6381"}, "names its report"),
        ({"from_role": "discord-6381"}, "cannot file in the agents inbox"),
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


def test_the_sweep_reports_a_broken_queue_file_once_at_its_content_and_files_no_request(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    world.write_raw("tasks/t-0042.md", "not a task")
    with caplog.at_level(logging.ERROR, logger=format_check.__name__):
        first = format_check.sweep(world.ctx())
        world.write_raw("tasks/t-0001.md", world.queue().files.read("tasks/t-0042.md").decode())  # another change
        again = format_check.sweep(world.ctx())
        world.write_raw("tasks/t-0042.md", "still not a task")
        changed = format_check.sweep(world.ctx())

    assert (first, changed) == (["tasks/t-0042.md"], ["tasks/t-0042.md"]) and "tasks/t-0042.md" not in again
    assert len([r for r in caplog.records if "t-0042" in r.getMessage()]) == 2
    assert world.queue().requests()[0] == []  # no agent could act on one


def test_the_sweep_reads_nothing_while_the_queue_is_unchanged_and_a_rewrite_is_read_again(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.write_raw("tasks/t-0042.md", "not a task")
    format_check.sweep(world.ctx())
    world.queue().blobs()  # the request the sweep filed is read once, like any write
    reads: list[str] = []
    real = QueueFiles.read
    monkeypatch.setattr(QueueFiles, "read", lambda self, rel: reads.append(rel) or real(self, rel))

    assert format_check.sweep(world.ctx()) == [] and reads == []

    world.queue().files.write("tasks/t-0042.md", b"still not a task")
    assert world.queue().blobs()["tasks/t-0042.md"] == b"still not a task"
    assert reads == ["tasks/t-0042.md"]


@pytest.mark.parametrize(
    ("text", "kind", "task", "reason"),
    [("task t-0001", "task", "t-0001", ""), ("joined t-0042", "joined", "t-0042", ""),
     ("parked waits on the billing rework", "parked", "", "waits on the billing rework"),
     ("noise a test message", "noise", "", "a test message"), ("", "waiting", "", "")],
)  # fmt: skip
def test_an_outcome_is_read_one_way_into_its_kind_task_and_reason(text: str, kind: str, task: str, reason: str) -> None:
    assert formats.outcome(text) == formats.Outcome(kind, task, reason)
