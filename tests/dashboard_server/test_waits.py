"""A benched implementer's panel says what its task waits on: the check of its merge's bundle, running on staging,
saved at a commit that has the merge in, or not started and where the merge waits; or, on hold, each task it waits
for and how that one's check stands."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from dashboard_rig import Rig
from dashboard_seed import held, merged, saved_check
from product_git import STANDING, ProductGit, run
from seed_kit import iso

from runtime.queue import turn
from runtime.shared import models
from runtime.warden.delivery import deploys
from runtime.warden.sessions import registry


@pytest.fixture
def product(rig: Rig, tmp_path: Path) -> ProductGit:
    found = ProductGit(tmp_path / "repo", {"phone-lists": STANDING, "first-invoice": STANDING})
    rig.warden.launcher.git = run
    return found


def _merge(rig: Rig, product: ProductGit, task_id: str) -> str:
    commit, _ = product.merge(f"web/{task_id}.js")
    merged(product.repo, rig.places, task_id, commit, iso(rig.clock.now() - 15 * 60))
    return commit


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
    ("saved", "walk", "tone", "says"),
    [
        ("own:fail", None, "bad", "Check failed on {own}, 1 h ago: phone-lists"),
        ("own:blocked", None, "warn", "Check blocked on {own}, 1 h ago: first-invoice"),
        ("own:pass", None, "good", "Check passed on {own}, 1 h ago"),
        ("later:fail", None, "bad", "Check failed on {later}, 1 h ago: phone-lists"),
        ("base:fail", None, "ring", "Not checked yet · in the next check"),
        (None, None, "ring", "Not checked yet · in the next check"),
        ("own:fail", "own", "accent", "Check running on {own}, started 10 min ago"),
        ("own:fail", "later", "accent", "Check running on {later}, started 10 min ago"),
        (None, "base", "ring", "Not checked yet · in the next check"),
    ],
    ids=[
        "failed",
        "blocked",
        "passed",
        "a-check-of-a-later-commit-has-it-in",
        "a-check-of-a-commit-before-it-has-it-not",
        "not-started",
        "running-on-staging-whatever-an-earlier-check-found",
        "running-on-a-later-build",
        "waits-while-testers-walk-a-build-before-it",
    ],
)
def test_a_merged_implementer_says_how_the_check_of_its_merges_bundle_stands(
    rig: Rig, product: ProductGit, saved: str | None, walk: str | None, tone: str, says: str
) -> None:
    commits = {"base": product.base, "own": _merge(rig, product, "t-0020"), "later": _merge(rig, product, "t-0021")}
    if saved is not None:
        at, result = saved.split(":")
        rows = {"first-invoice": result} if result == "blocked" else {"phone-lists": result, "first-invoice": "pass"}
        saved_check(product.repo, commits[at], rig.clock.now() - 3600, rows)
    if walk is not None:
        deploys.record(rig.places, "staging", commits[walk][:8], iso(rig.clock.now() - 11 * 60))
        _walking(rig, 10)

    text = says.format(**{name: commit[:7] for name, commit in commits.items()})
    assert _waits(rig, "impl-project-names") == [{"task": None, "title": None, "tone": tone, "text": text}]


def test_a_merge_past_the_next_bundle_says_how_many_merges_are_ahead_of_it(rig: Rig, product: ProductGit) -> None:
    with rig.places.overrides.open("a") as overrides:
        overrides.write("\n[rounds]\nbundle_merges = 1\n")
    _merge(rig, product, "t-0021")
    _merge(rig, product, "t-0020")

    assert [w["text"] for w in _waits(rig, "impl-project-names")] == ["Not checked yet · 1 merge ahead of it"]


def test_an_implementer_on_hold_names_each_task_it_waits_for_and_how_that_ones_check_stands(
    rig: Rig, product: ProductGit
) -> None:
    commit = _merge(rig, product, "t-0021")
    saved_check(product.repo, commit, rig.clock.now() - 3600, {"phone-lists": "fail"})
    held(product.repo, rig.places, "t-0020", ["t-0021", "t-0017", "t-0099"], iso(rig.clock.now() - 15 * 60))

    assert _waits(rig, "impl-project-names") == [
        {"task": "t-0021", "title": "Staging is down after the last deploy", "tone": "bad",
         "text": f"Check failed on {commit[:7]}, 1 h ago: phone-lists"},
        {"task": "t-0017", "title": "Sign-in sends people back to the sign-in page", "tone": "accent",
         "text": "QA, round 2 of 4"},
        {"task": None, "title": "t-0099", "tone": "ring", "text": "Not in the queue"},
    ]  # fmt: skip


@pytest.mark.parametrize("agent", ["impl-staging-down", "impl-project-names", "qa-ui-1", "orchestrator"])
def test_an_agent_whose_task_waits_on_no_check_shows_no_wait(rig: Rig, agent: str) -> None:
    assert _waits(rig, agent) == []


def test_a_merged_task_whose_staging_build_is_unknown_says_its_check_may_be_running(
    rig: Rig, product: ProductGit
) -> None:
    _merge(rig, product, "t-0020")
    _walking(rig, 10)
    rig.places.deploys.write_text("")

    [wait] = _waits(rig, "impl-project-names")

    assert wait["text"] == "Its check may be running: which build staging runs is not known"


@pytest.mark.parametrize("found", ["no-merge", "no-repo"])
def test_a_merged_task_whose_merge_cannot_be_placed_says_so(rig: Rig, product: ProductGit, found: str) -> None:
    _merge(rig, product, "t-0020")
    if found == "no-merge":
        turn.merges_path(rig.places).write_text("{}")
    else:
        rig.warden.launcher.git = lambda argv: (128, "fatal: not a git repository")

    [wait] = _waits(rig, "impl-project-names")

    said = {"no-merge": "Its merge is not found on the default branch", "no-repo": "Its check cannot be read. git "}
    assert wait["text"].startswith(said[found])
