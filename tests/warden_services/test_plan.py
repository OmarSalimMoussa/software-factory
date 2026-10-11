from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from runtime.warden.context import Context
from runtime.warden.spend import plan

WINDOWS = {"five_hour": {"used_percentage": 12, "resets_at": 1791558000}, "seven_day": None}


def _iso(at: float) -> str:
    return datetime.fromtimestamp(at, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.mark.parametrize(("written_ago_minutes", "state"), [(5, "fresh"), (120, "stale")])
def test_a_reading_ages_by_its_own_time_never_the_files(
    ctx: Context, tmp_path: Path, written_ago_minutes: int, state: str
) -> None:
    source = plan.source(tmp_path / "agent", ctx.places)
    source.parent.mkdir(parents=True)
    # Written (and so modified) just now, but stamped by the status line with an older time of its own.
    source.write_text(json.dumps({**WINDOWS, "written_at": _iso(ctx.clock.now() - written_ago_minutes * 60)}))

    plan.PlanWatch(source).run_pass(ctx)
    reading = plan.reading(ctx)

    assert reading["state"] == state
    assert reading["age_seconds"] == written_ago_minutes * 60
    assert reading["five_hour"] == (WINDOWS["five_hour"] if state == "fresh" else None)


def test_a_link_planted_in_place_of_the_reading_is_not_followed(ctx: Context, tmp_path: Path) -> None:
    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps({**WINDOWS, "written_at": _iso(ctx.clock.now())}))
    source = plan.source(tmp_path / "agent", ctx.places)
    source.parent.mkdir(parents=True)
    source.symlink_to(secret)

    plan.PlanWatch(source).run_pass(ctx)

    assert plan.reading(ctx)["state"] == "missing"
