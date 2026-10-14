"""A finished tester's runs move into the warden's evidence by task, head and tester; no link is followed."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from queue_kit import World

from runtime.qa import evidence, qa_browser

SESSION = "0b6f1c3e-1d2a-4c5b-9e8f-7a6b5c4d3e2f"
HEAD = "c" * 40


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


def test_two_testers_of_one_kind_at_once_each_take_only_their_own_lanes_run(world: World, tmp_path: Path) -> None:
    runs, at = tmp_path / "runs", datetime(2026, 10, 9, 12, 5, tzinfo=UTC)
    first, second = (qa_browser.prepare(lane, SESSION, runs, world.ctx().settings(), at).parent for lane in
                     ("qa-ux", "qa-ux-2"))  # fmt: skip

    taken = evidence.intake(world.ctx(), runs, SESSION, "t-0001", HEAD, "qa-ux-2", "ag-2", "2026-10-09T12:00:00Z")

    assert taken == [second.name] and first.is_dir()
    assert (world.places.evidence / "t-0001" / HEAD[:12] / "qa-ux" / second.name / "run.json").is_file()
