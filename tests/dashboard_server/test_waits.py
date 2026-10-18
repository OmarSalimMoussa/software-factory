"""A benched implementer's panel says what its task waits on: its merge's check, running on staging, saved or not
started, on which commit; or, on hold, each task it waits for and how that one's check stands."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from dashboard_rig import Rig
from dashboard_seed import STAGING, held, merged
from seed_kit import iso

from runtime.shared import models
from runtime.warden.sessions import registry

# Seeded hourly checks: a1b2c3d failed phone-lists, 77ab12e blocked first-invoice, 3e9f0aa passed; staging is a1b2c3d.
FAILED, BLOCKED, PASSED, UNCHECKED = (sha.ljust(40, "0") for sha in (STAGING, "77ab12e", "3e9f0aa", "4d5e6f7"))


def _waits(rig: Rig, agent: str) -> list[dict[str, Any]]:
    waits: list[dict[str, Any]] = rig.parts(f"/api/agent/{agent}")["agent"]["data"]["waits"]
    return waits


def _walking(rig: Rig, minutes_ago: int) -> None:
    """A regression tester the orchestrator started, walking staging now."""
    sessions = registry.read(rig.places)
    at = iso(rig.clock.now() - minutes_ago * 60)
    sessions["reg-1"] = models.Session(
        id="reg-1", name="reg-1", role="qa-regression", family="qa", model="claude-sonnet-5-5", effort="high",
        task="", parent="orchestrator", worktree="", state=models.WORKING, started=at, started_by="orchestrator",
        agent_id="agent-reg-1", started_as="qa-regression", since=at,
    )  # fmt: skip
    registry.write(rig.places, sessions)


@pytest.mark.parametrize(
    ("commit", "walk", "tone", "says"),
    [
        (FAILED, None, "bad", "Check failed on a1b2c3d, 1 h ago: phone-lists"),
        (BLOCKED, None, "warn", "Check blocked on 77ab12e, 2 h ago: first-invoice"),
        (PASSED, None, "good", "Check passed on 3e9f0aa, 3 h ago"),
        (UNCHECKED, None, "ring", "Check not started on 4d5e6f7, merged 15 min ago"),
        (FAILED, 10, "accent", "Check running on a1b2c3d, started 10 min ago"),
        (FAILED, 100, "accent", "Check running on a1b2c3d, started 2 h ago"),
        (UNCHECKED, 10, "ring", "Check not started on 4d5e6f7, merged 15 min ago"),
    ],
    ids=[
        "failed",
        "blocked",
        "passed",
        "not-started",
        "running-on-staging-whatever-an-earlier-check-found",
        "running-since-before-a-check-its-walk-saved",
        "waits-while-testers-walk-another-build",
    ],
)
def test_a_merged_implementer_says_how_its_merges_check_stands(
    rig: Rig, tmp_path: Path, commit: str, walk: int | None, tone: str, says: str
) -> None:
    merged(tmp_path / "repo", rig.places, "t-0020", commit, iso(rig.clock.now() - 15 * 60))
    if walk is not None:
        _walking(rig, walk)

    assert _waits(rig, "impl-project-names") == [{"task": None, "title": None, "tone": tone, "text": says}]


def test_an_implementer_on_hold_names_each_task_it_waits_for_and_how_that_ones_check_stands(
    rig: Rig, tmp_path: Path
) -> None:
    repo, at = tmp_path / "repo", iso(rig.clock.now() - 15 * 60)
    merged(repo, rig.places, "t-0021", FAILED, at)
    held(repo, rig.places, "t-0020", ["t-0021", "t-0017", "t-0099"], at)

    assert _waits(rig, "impl-project-names") == [
        {"task": "t-0021", "title": "Staging is down after the last deploy", "tone": "bad",
         "text": "Check failed on a1b2c3d, 1 h ago: phone-lists"},
        {"task": "t-0017", "title": "Sign-in sends people back to the sign-in page", "tone": "accent",
         "text": "QA, round 2 of 4"},
        {"task": None, "title": "t-0099", "tone": "ring", "text": "Not in the queue"},
    ]  # fmt: skip


@pytest.mark.parametrize("agent", ["impl-staging-down", "impl-project-names", "qa-ui-1", "orchestrator"])
def test_an_agent_whose_task_waits_on_no_check_shows_no_wait(rig: Rig, agent: str) -> None:
    assert _waits(rig, agent) == []


def test_a_merged_task_whose_staging_build_is_unknown_says_its_check_may_be_running(rig: Rig, tmp_path: Path) -> None:
    merged(tmp_path / "repo", rig.places, "t-0020", UNCHECKED, iso(rig.clock.now() - 15 * 60))
    _walking(rig, 10)
    rig.places.deploys.write_text("")

    [wait] = _waits(rig, "impl-project-names")

    assert wait["text"] == "Check on 4d5e6f7 may be running: which build staging runs is not known"


def test_a_merged_task_whose_log_cannot_be_read_says_its_check_cannot_be_read(rig: Rig, tmp_path: Path) -> None:
    merged(tmp_path / "repo", rig.places, "t-0020", FAILED, iso(rig.clock.now() - 15 * 60))
    with (tmp_path / "repo" / "queue" / "tasks" / "t-0020.log.jsonl").open("a") as log:
        log.write("not a log line\n")

    [wait] = _waits(rig, "impl-project-names")

    assert wait["text"].startswith("Its check cannot be read. The warden could not read it: ")
