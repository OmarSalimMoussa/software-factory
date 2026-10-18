"""The CPU hold on new walks: a browser tester waits while the machine's average CPU use is past
budget.walk_hold_percent or unknown, and starts anyway once its session has waited budget.walk_hold_minutes."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from guard_kit import NOW, SID, Kit, agent_use, entry, result

from runtime.guard import hook, wire
from runtime.guard.facts import Refusal
from runtime.shared import store
from runtime.shared.paths import SLICE

START = {"subagent_type": "qa-ux", "prompt": "Scenarios and checks: 1"}
ANSWERS = {
    "held": wire.one_line("cpu-hold", "the machine's CPU use averages 80%"),
    "capped": wire.one_line("subagents-at-once", "4 running"),
    "started": "Async agent launched. agentId: a1b2c3",
}


def _kit(tmp_path: Path, *off: str) -> Kit:
    kit = Kit(tmp_path)
    kit.session("implementer")
    kit.settings(budget__walk_hold_percent=65.0, budget__walk_hold_minutes=15, budget__cpu_window_seconds=300)
    kit.switch_off("qa-ready", *off)
    return kit


@pytest.mark.parametrize(
    ("kind", "cpu", "age", "off", "said"),
    [
        ("qa-ux", 50.0, 0, False, None),
        ("qa-ux", 65.0, 0, False, None),
        ("qa-ux", 80.0, 0, False, "averages 80%, past the 65% at which new walks wait"),
        ("qa-ux", None, 0, False, "unknown (the warden could not read it)"),
        ("qa-ux", 50.0, 301, False, "unknown (its last reading is 5 min old)"),
        ("qa-ux", 50.0, None, False, "unknown (the warden has published no reading)"),
        ("qa-backend", 80.0, 0, False, None),
        ("qa-ux", 80.0, 0, True, None),
    ],
    ids=["under", "at-the-hold", "past-the-hold", "unread", "stale", "none-yet", "no-browser", "switched-off"],
)
def test_a_browser_tester_waits_while_the_machines_cpu_use_is_past_the_hold_or_unknown(
    tmp_path: Path, kind: str, cpu: float | None, age: int | None, off: bool, said: str | None
) -> None:
    kit = _kit(tmp_path, *(["cpu-hold"] if off else []))
    kit.vitals(cpu=cpu, age=age or 0)
    if age is None:
        (kit.public / "vitals.json").unlink()
    decision = kit.decide("Agent", {**START, "subagent_type": kind})
    reason = decision.reason if isinstance(decision, Refusal) and decision.rule == "cpu-hold" else None
    assert (reason is None) is (said is None)
    assert said is None or (reason is not None and said in reason and "wait or do other work" in reason)


@pytest.mark.parametrize(
    ("history", "held"),
    [
        ([], True),
        ([("t1", 10, "held")], True),
        ([("t1", 16, "held")], False),
        ([("t1", 20, "held"), ("t2", 12, "held")], False),
        ([("t1", 20, "held"), ("t2", 5, "started")], True),
        ([("t1", 20, "held"), ("t2", 0, "sent")], True),
        ([("t1", 20, "capped")], True),
    ],
    ids=[
        "first-try",
        "held-10-min",
        "held-16-min",
        "counted-from-the-first",
        "a-start-since",
        "one-sent-before-it",
        "refused-by-another-rule",
    ],
)
def test_a_held_browser_tester_starts_once_its_session_has_waited_the_longest_hold(
    tmp_path: Path, history: list[tuple[str, int, str]], held: bool
) -> None:
    kit = _kit(tmp_path)
    kit.vitals(cpu=80.0)
    lines = []
    for use_id, ago, how in history:
        lines.append(entry(NOW - timedelta(minutes=ago), "assistant", [agent_use(use_id, "qa-visual")]))
        if how != "sent":
            lines.append(entry(NOW - timedelta(minutes=ago), "user", [result(use_id, ANSWERS[how])]))
    kit.transcript(lines)
    assert (kit.rule("Agent", START) == "cpu-hold") is held


@pytest.mark.parametrize(
    "record",
    [{"level": "green", "why": ""}, {"level": "green", "why": "", "cpu": 30.0, "at": "soon"}],
    ids=["without-cpu-use", "bad-time"],
)
def test_vitals_the_guard_cannot_read_refuse_a_browser_testers_start(tmp_path: Path, record: dict[str, object]) -> None:
    kit = _kit(tmp_path)
    store.write_json(kit.public / "vitals.json", record)
    raw = json.dumps(kit.payload("Agent", START))
    code, _, err = hook.handle(raw, f"0::/{SLICE}/agent-run@{SID}.service\n", kit.places, kit.home, NOW)
    assert code == 2 and err.startswith("Refused by the factory guard (guard): the guard failed (StoreError")
