from __future__ import annotations

from typing import Any

import pytest
from queue_kit import World

from runtime.queue import records
from runtime.queue.book import log_rel
from runtime.shared import models, store
from runtime.shared.models import RETIRED, Session, TimeRecord
from runtime.warden import snapshot
from runtime.warden.sessions import registry

DAY = "2026-10-09T"
SHA = "a1b2c3d" + "0" * 33
MOVED = {"actor": "warden", "event": "move", "text": "moved"}


def journey(world: World, *moves: tuple[str, str]) -> str:
    task = world.task()
    lines = [{**MOVED, "at": DAY + at + ":00Z", "to": to} for at, to in moves]
    world.write_raw(log_rel(task.id), "".join(records.dump_log(entry).decode() for entry in lines))
    return task.id


def time_part(world: World, *tasks: str) -> dict[str, Any]:
    answer = snapshot.snapshot(world.owner(), {"want": ["time"], "tasks": list(tasks)}, None)
    assert answer["errors"] == {}
    found: dict[str, Any] = answer["state"]["time"]
    return found


def spent(world: World, task: str, session: str, subagent: str = "", whole: bool = True, **parts: Any) -> None:
    record = TimeRecord(DAY + "05:00:00Z", DAY + "06:00:00Z", session, subagent, "qa-ux" if subagent else "implementer",
                        "qa" if subagent else "impl", task, parts.get("model", 0.0), parts.get("tools", {}),
                        parts.get("waiting", 0.0), parts.get("idle", 0.0), whole)  # fmt: skip
    store.append_jsonl(world.places.times, models.dump(record))


def test_a_tasks_wall_time_is_told_per_step_and_per_status_summed_over_its_visits_the_last_one_so_far(
    world: World,
) -> None:
    task = journey(
        world, ("00:00", "queued"), ("01:00", "proposed"), ("02:00", "approved"), ("02:10", "designing"),
        ("03:00", "review"), ("03:30", "building"), ("03:40", "deprioritized"), ("03:45", "building"), ("04:00", "qa"),
        ("04:30", "qa-clean"), ("04:31", "turn"), ("04:40", "merged"), ("06:00", "checked"), ("06:05", "on-staging"),
    )  # fmt: skip

    found = time_part(world, task)["tasks"][task]

    assert found["legs"] == {
        "total": 14700.0, "queued": 3600.0, "approval": 3600.0, "start": 600.0, "build": 6600.0, "qa": 1800.0,
        "turn": 60.0, "merge": 540.0, "check": 4800.0, "staging": 300.0,
    }  # fmt: skip
    assert [(s["status"], s["seconds"], s["now"]) for s in found["statuses"]] == [
        ("queued", 3600.0, False), ("proposed", 3600.0, False), ("approved", 600.0, False),
        ("designing", 3000.0, False), ("review", 1800.0, False), ("building", 1500.0, False),
        ("deprioritized", 300.0, False), ("qa", 1800.0, False), ("qa-clean", 60.0, False), ("turn", 540.0, False),
        ("merged", 4800.0, False), ("checked", 300.0, False), ("on-staging", 21300.0, True),
    ]  # fmt: skip


@pytest.mark.parametrize(
    ("spends", "runtime"),
    [
        (
            [
                ("s1", "", True, {"model": 60.0, "tools": {"Bash factory check": 40.0}, "waiting": 30.0, "idle": 20.0}),
                ("s1", "", True, {"model": 40.0, "tools": {"Read": 10.0}}),
                ("s1", "q1", True, {"model": 25.0, "tools": {"Bash factory check": 30.0}, "idle": 5.0}),
            ],
            {
                "lead": {"model": 100.0, "tools": 50.0, "waiting": 30.0, "idle": 20.0, "alive": 200.0},
                "helpers": {"model": 25.0, "tools": 30.0, "waiting": 0.0, "idle": 5.0, "alive": 60.0},
                "tools": [["Bash factory check", 70.0], ["Read", 10.0]],
            },
        ),
        ([], None),
        ([("s1", "q1", True, {"model": 25.0})], None),
        ([("s1", "", True, {"model": 60.0}), ("s1", "q1", False, {"model": 25.0})], None),
    ],
    ids=["measured", "no-transcript", "no-lead", "a-transcript-not-whole"],
)
def test_a_tasks_runtime_keeps_its_lead_and_its_subagents_apart_and_is_unknown_unless_measured_whole(
    world: World, spends: list[tuple[str, str, bool, dict[str, Any]]], runtime: dict[str, Any] | None
) -> None:
    task = journey(world, ("02:00", "approved"), ("02:10", "building"))
    for session, subagent, whole, parts in spends:
        spent(world, task, session, subagent, whole, **parts)

    assert time_part(world, task)["tasks"][task]["runtime"] == runtime


def _walk(world: World, agent: str, started: str, ended: str) -> None:
    sessions = registry.read(world.places)
    sessions[agent] = Session(agent, f"orchestrator.qa-regression-{agent}", "qa-regression", "qa", "m", "high", "",
                              "s-orch", "", RETIRED, DAY + started + ":00Z", "s-orch", ended=DAY + ended + ":00Z",
                              agent_id=agent)  # fmt: skip
    registry.write(world.places, sessions)


def _check(world: World, at: str, result: str) -> None:
    check_id = f"c-20261009T{at.replace(':', '')}00Z-{SHA[:7]}"
    row = records.ScenarioRow("home-loads", result, 3, "evidence/x")
    record = records.CheckRecord(check_id, SHA, DAY + at + ":00Z", "The default branch", [row], "Walked.")
    world.write_raw(f"checks/{check_id}.md", records.dump_check(record))


def test_a_merges_wait_for_its_check_splits_into_testers_walking_waiting_and_after_a_failed_check(
    world: World,
) -> None:
    task = journey(world, ("02:00", "approved"), ("04:40", "merged"), ("06:00", "checked"), ("06:05", "on-staging"))
    _walk(world, "w1", "04:30", "04:50")
    _check(world, "04:52", "fail")
    _walk(world, "w2", "05:20", "05:30")
    _check(world, "06:00", "pass")

    found = time_part(world)

    assert found["tasks"][task]["check"] == {
        "walking": 1200.0, "waiting": 1920.0, "after_failure": 1680.0, "checks": 2, "failed": 1,
    }  # fmt: skip
    assert found["summary"]["walks"]["n"] == 2
    assert found["summary"]["checks"]["merges"] == 1


def test_the_summary_tells_each_steps_median_and_p75_over_the_tasks_that_reached_staging_in_the_window(
    world: World,
) -> None:
    quick = journey(world, ("02:00", "approved"), ("03:00", "on-staging"))
    slow = journey(world, ("02:00", "approved"), ("06:00", "on-staging"))
    middle = journey(world, ("02:00", "approved"), ("04:00", "on-staging"))
    task = world.task()
    world.write_raw(
        log_rel(task.id), records.dump_log({**MOVED, "at": "2026-09-29T02:00:00Z", "to": "on-staging"}).decode()
    )
    spent(world, quick, "s1", model=60.0, idle=30.0)

    summary = time_part(world)["summary"]

    total = next(leg for leg in summary["legs"] if leg["key"] == "total")
    assert (summary["tasks"], total["n"], total["median"], total["p75"]) == (3, 3, 7200.0, 10800.0)
    assert summary["slowest"] == [slow, middle, quick]
    assert summary["runtime"]["tasks"] == 1
    assert summary["runtime"]["lead"]["alive"] == {"median": 90.0, "p75": 90.0}
