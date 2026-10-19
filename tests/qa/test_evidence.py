"""A finished tester's runs move into the warden's evidence by task, head and tester, and the regression testers'
by the check they walked for; no link is followed."""

from __future__ import annotations

import dataclasses
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest
from queue_kit import World

from runtime.qa import evidence, qa_browser
from runtime.queue import records, verbs
from runtime.shared.models import RETIRED
from runtime.warden import snapshot
from runtime.warden.sessions import registry

SESSION = "0b6f1c3e-1d2a-4c5b-9e8f-7a6b5c4d3e2f"
HEAD = "c" * 40
ORCH, BEFORE = "1d4e7a90-3b2c-4f5e-8a6b-0c9d8e7f6a5b", "2e5f8b01-4c3d-4a6f-9b7c-1d0e9f8a7b6c"  # its id, then its last
MERGE = "abc1234" + "0" * 33
REPORT = f"# Check\n\n## Scenarios\n\n{records.CHECK_TABLE}\n| --- | --- | --- | --- |\n| sign-in | pass | 3 | x |\n"
RUN = "20261009T114001Z-0a1b2c"  # started at 11:40:01; the check is saved at 12:00:00


def test_a_testers_runs_are_copied_without_following_links_or_a_replay_and_taken_once(
    world: World, tmp_path: Path
) -> None:
    runs = tmp_path / "runs"
    run = runs / SESSION / "20261009T120500Z-0a1b2c"
    (run / "video").mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({"role": "qa-ux", "started": "2026-10-09T12:05:00Z"}))
    (run / "video" / "page.webm").write_bytes(b"video")
    (run / "replay").mkdir()
    (run / "replay" / "replay.json").write_text('{"version": 1, "error": "planted"}')  # the warden's to write
    (tmp_path / "secret").write_text("key")
    (run / "secret").symlink_to(tmp_path / "secret")
    other = runs / SESSION / "20261009T120600Z-0d0e0f"
    other.mkdir()
    (other / "run.json").write_text(json.dumps({"role": "qa-visual", "started": "2026-10-09T12:06:00Z"}))

    taken = evidence.intake(world.ctx(), runs, SESSION, "t-0001", HEAD, "qa-ux", "ag-ux", "2026-10-09T12:00:00Z")
    target = world.places.evidence / "t-0001" / HEAD[:12] / "qa-ux" / run.name
    assert taken == [run.name]
    assert (target / "video" / "page.webm").read_bytes() == b"video"
    assert not (target / "secret").exists() and not (target / "replay").exists()
    assert world.launcher.commands == [["rm", "-rf", "--", str(run)]]
    assert evidence.intake(world.ctx(), runs, SESSION, "t-0001", HEAD, "qa-ux", "ag-ux", "2026-10-09T12:00:00Z") == []


def test_a_page_is_sent_a_runs_screenshots_never_the_files_of_its_trace(world: World, tmp_path: Path) -> None:
    run = tmp_path / "runs" / SESSION / "20261009T120500Z-0a1b2c"
    (run / "traces" / "resources").mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({"role": "qa-ux", "started": "2026-10-09T12:05:00Z"}))
    for name in ("page-1.png", "page-1.yml", "traces/resources/page@1.jpeg"):
        (run / name).write_bytes(b"x")
    evidence.intake(world.ctx(), run.parent.parent, SESSION, "t-0001", HEAD, "qa-ux", "ag", "2026-10-09T12:00:00Z")

    answer = snapshot.snapshot(world.ctx(), {"want": ["qa"], "tasks": ["t-0001"]}, None)

    [entry] = answer["state"]["qa"]["t-0001"]["evidence"]
    assert (entry["shots"], "files" in entry) == (["page-1.png"], False)


def test_two_testers_of_one_kind_at_once_each_take_only_their_own_lanes_run(world: World, tmp_path: Path) -> None:
    runs, at = tmp_path / "runs", datetime(2026, 10, 9, 12, 5, tzinfo=UTC)
    first, second = (qa_browser.prepare(lane, SESSION, runs, world.ctx().settings(), at).parent for lane in
                     ("qa-ux", "qa-ux-2"))  # fmt: skip

    taken = evidence.intake(world.ctx(), runs, SESSION, "t-0001", HEAD, "qa-ux-2", "ag-2", "2026-10-09T12:00:00Z")

    assert taken == [second.name] and first.is_dir()
    assert (world.places.evidence / "t-0001" / HEAD[:12] / "qa-ux" / second.name / "run.json").is_file()


def test_a_run_whose_agent_copy_stays_is_still_taken_and_the_warden_says_so(
    world: World, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    runs = tmp_path / "runs"
    (runs / SESSION / RUN).mkdir(parents=True)
    (runs / SESSION / RUN / "run.json").write_text(json.dumps({"role": "qa-ux", "started": "2026-10-09T11:40:01Z"}))
    world.launcher.git = lambda argv: (1, "rm: Permission denied")

    with caplog.at_level(logging.WARNING, logger="factory.warden"):
        assert evidence.intake(world.ctx(), runs, SESSION, "t-0001", HEAD, "qa-ux", "ag", "2026-10-09T11:00:00Z")

    assert f"the agent's copy of QA run {RUN} could not be removed (exit 1)" in caplog.text


@pytest.fixture
def orchestrator(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    """The orchestrator, run on as a copy once, whose checkout knows the merge its check names, as the default
    branch's one commit, with no scenario yet."""
    world.session(ORCH, "orchestrator", "orchestrator")
    sessions = registry.read(world.places)
    sessions[ORCH] = dataclasses.replace(sessions[ORCH], copied_from=BEFORE)
    registry.write(world.places, sessions)
    world.launcher.git = lambda argv: (1, "") if "grep" in argv else (0, f"{MERGE}\n")
    monkeypatch.setattr(qa_browser, "runs_root", lambda: tmp_path / "runs")
    return world


def _tester(world: World, lane: str, started: str, ended: str) -> None:
    """A regression tester the orchestrator started in ``lane``; it walks still when it has not ``ended``."""
    world.session("ag-1", "qa-regression", f"orchestrator.{lane}", agent_id="ag-1", parent=ORCH)
    sessions = registry.read(world.places)
    walked = {"state": RETIRED, "retired": "finished", "ended": ended} if ended else {}
    sessions["ag-1"] = dataclasses.replace(sessions["ag-1"], started_as=lane, started=started, **walked)
    registry.write(world.places, sessions)


@pytest.mark.parametrize(
    ("lane", "folder", "tester", "before", "agent"),
    [
        ("qa-regression-2", ORCH, ("qa-regression-2", "11:40:00", "11:50:00"), None, "ag-1"),
        ("qa-regression", BEFORE, ("qa-regression", "11:40:00", "11:50:00"), None, "ag-1"),
        ("qa-regression", ORCH, ("qa-regression", "11:40:00", "11:50:00"), "11:45:00", "ag-1"),
        ("qa-regression", ORCH, None, None, None),
        ("qa-regression", ORCH, ("qa-regression", "11:40:00", ""), None, "left"),
        ("qa-regression", ORCH, ("qa-regression", "11:41:00", ""), None, "left"),
        ("qa-regression", ORCH, ("qa-regression", "11:40:00", "11:50:00"), "11:55:00", "left"),
        ("qa-ux", ORCH, None, None, "left"),
    ],
    ids=[
        "its-testers-in-its-lane",
        "made-before-the-orchestrator-ran-on-as-a-copy",
        "walk-ended-after-the-check-before",
        "no-tester-known-has-no-agent",
        "its-tester-still-walks",
        "no-tester-known-while-its-lane-walks",
        "walk-ended-before-the-check-before",
        "a-task-testers-run",
    ],
)
def test_a_saved_check_takes_the_regression_runs_it_reports_on_each_with_its_tester(
    orchestrator: World,
    tmp_path: Path,
    lane: str,
    folder: str,
    tester: tuple[str, str, str] | None,
    before: str | None,
    agent: str | None,
) -> None:
    world, day = orchestrator, "2026-10-09T"
    run = tmp_path / "runs" / folder / RUN
    run.mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({"role": lane, "session_id": folder, "started": f"{day}11:40:01Z"}))
    (run / "page-1.png").write_bytes(b"png")
    if tester is not None:
        _tester(world, tester[0], f"{day}{tester[1]}Z", f"{day}{tester[2]}Z" if tester[2] else "")
    if before is not None:
        earlier = f"c-20261009T{before.replace(':', '')}Z-abc1234"
        rows = [records.ScenarioRow("sign-in", "pass", 3, "x")]
        record = records.CheckRecord(earlier, MERGE, f"{day}{before}Z", "Earlier", rows)
        world.write_raw(f"checks/{record.id}.md", records.dump_check(record))

    answer = verbs.check_save(world.ctx("orchestrator", ORCH), {"commit": "abc1234"}, REPORT.encode())

    if agent == "left":
        assert answer["runs"] == [] and run.is_dir() and not evidence.entries(world.places)
        return
    assert answer["check"] == "c-20261009T120000Z-abc1234" and answer["runs"] == [RUN]
    [entry] = evidence.entries(world.places)
    assert sorted(entry.pop("files")) == ["page-1.png", "run.json"]
    names = {"check": answer["check"], "head": MERGE, "tester": "qa-regression", "run": RUN, "session": folder}
    assert entry == {**names, "agent": agent, "at": f"{day}12:00:00Z"}
    assert (evidence.folder(world.places, entry) / "page-1.png").read_bytes() == b"png"
    assert world.launcher.commands[-1] == ["rm", "-rf", "--", str(run)]


def test_a_check_whose_runs_cannot_be_taken_is_saved_and_the_warden_says_so(
    orchestrator: World, caplog: pytest.LogCaptureFixture
) -> None:
    orchestrator.places.evidence.parent.mkdir(parents=True, exist_ok=True)
    orchestrator.places.evidence.write_text("not a folder")

    with caplog.at_level(logging.WARNING, logger="factory.warden"):
        answer = verbs.check_save(orchestrator.ctx("orchestrator", ORCH), {"commit": "abc1234"}, REPORT.encode())

    assert answer["runs"] is None and [c.id for c in orchestrator.queue().checks()[0]] == [answer["check"]]
    assert f"the runs of check {answer['check']} could not be taken into the evidence (StoreError)" in caplog.text
