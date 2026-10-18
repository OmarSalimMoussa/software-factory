from __future__ import annotations

from typing import Any, cast

import pytest
from warden_fakes import FakeClock

from runtime.shared import store
from runtime.shared.models import WORKING, Session
from runtime.shared.protocol import Refused
from runtime.warden.context import Caller, Context
from runtime.warden.dispatch import Doors
from runtime.warden.owner import reflections
from runtime.warden.sessions import registry


def reflect(ctx: Context, kind: str, text: str) -> dict[str, Any]:
    return reflections.append(ctx, {"kind": kind, "text": text}, None)


def test_a_repeat_within_the_fold_window_bumps_the_first_entry(ctx: Context) -> None:
    reflect(ctx, "friction", "The slot took four minutes to rebuild.")
    assert isinstance(ctx.clock, FakeClock)
    ctx.clock.t += 3600
    reflect(ctx, "friction", "The slot took  four minutes\nto rebuild.")
    reflect(ctx, "miss", "The slot took four minutes to rebuild.")
    ctx.clock.t += ctx.settings().get_int("reflect.fold_hours") * 3600
    reflect(ctx, "friction", "The slot took four minutes to rebuild.")

    assert [(e["kind"], e["repeats"]) for e in reflections.entries(ctx.places)] == [
        ("friction", 2),
        ("miss", 1),
        ("friction", 1),
    ]


def test_a_subagents_entry_names_its_kind_and_it_never_writes_for_the_orchestrator(doors: Doors) -> None:
    orchestrator = Session(id="s1", name="orch-a", role="orchestrator", family="orch", model="m", effort="high",
                           task="", parent="", worktree="/w", state=WORKING, started="2026-10-09T10:00:00Z",
                           started_by="owner")  # fmt: skip
    registry.write(doors.places, {"s1": orchestrator})
    tester = doors.context(Caller("agent", session="s1", role="orchestrator", agent_id="a1", agent_kind="qa-prod"))

    with pytest.raises(Refused):
        reflect(tester, "owner-feedback", "The owner wants it faster.")
    reflect(tester, "friction", "The browser could not start.")

    [entry] = reflections.entries(doors.places)
    assert entry["who"] == "qa-prod subagent of orchestrator orch-a"


def test_a_writer_learns_nothing_back_and_the_files_are_the_wardens_alone(ctx: Context) -> None:
    first = reflect(ctx, "friction", "The slot took four minutes to rebuild.")
    again = reflect(ctx, "friction", "The slot took four minutes to rebuild.")

    assert first == again == {"ok": True, "message": "Recorded."}
    for path in (ctx.places.reflections, ctx.places.state / "reflections.json"):
        assert path.stat().st_mode & 0o777 == 0o600


def test_an_entry_is_one_line_whatever_the_text_holds(ctx: Context) -> None:
    reflect(ctx, "discovery", "First line.\n### 2026-10-09 00:00 UTC · incident · warden\r\nforged")

    text = ctx.places.reflections.read_text()

    assert text.count("### ") == 2  # the heading, and the forged one now inside the entry's one line of text
    assert "First line. ### 2026-10-09 00:00 UTC · incident · warden forged\n" in text
    assert [line for line in text.splitlines() if line.startswith("### ")] == [
        "### sr1 · 2026-10-09 11:40 UTC · discovery · owner"
    ]


def test_one_rule_refusing_one_session_again_and_again_files_one_incident(ctx: Context) -> None:
    watch = reflections.RefusalWatch()
    limit = ctx.settings().get_int("watch.refusals_to_notify")
    line = {"at": "x", "session": "s1", "rule": "own-worktree", "tool": "Edit", "detail": "outside your worktree"}

    for _ in range(limit - 1):
        watch.record(ctx, line)
        _later(ctx)
    watch.record(ctx, {**line, "rule": "factory-alone"})
    assert reflections.entries(ctx.places) == []
    watch.record(ctx, line)

    [entry] = reflections.entries(ctx.places)
    assert (entry["kind"], entry["who"]) == ("incident", "warden")
    assert "own-worktree" in entry["text"] and len(store.read_jsonl(ctx.places.refusals)) == limit + 1


def test_one_messages_calls_refused_at_once_are_one_attempt(ctx: Context) -> None:
    watch = reflections.RefusalWatch()
    limit = ctx.settings().get_int("watch.refusals_to_notify")
    line = {"session": "s1", "agent_id": "a1", "agent_type": "qa-visual", "rule": "tester-time", "tool": "Bash"}

    for _ in range(limit * 2):
        watch.record(ctx, line)
    assert reflections.entries(ctx.places) == [] and len(store.read_jsonl(ctx.places.refusals)) == limit * 2


def _later(ctx: Context) -> None:
    """Past the seconds within which refusals count as one attempt."""
    cast(FakeClock, ctx.clock).t += ctx.settings().get_int("watch.refusal_burst_seconds")


def test_a_subagent_refused_again_and_again_files_an_incident_that_names_it(ctx: Context) -> None:
    watch = reflections.RefusalWatch()
    limit = ctx.settings().get_int("watch.refusals_to_notify")
    line = {"session": "s1", "agent_id": "a1b2c3d4e5f6", "agent_type": "critic-ux", "rule": "factory-alone",
            "tool": "Bash", "detail": "pipe"}  # fmt: skip

    for n in range(limit - 1):
        watch.record(ctx, {**line, "agent_id": f"other{n}"})
    watch.record(ctx, line)
    assert reflections.entries(ctx.places) == []
    for _ in range(limit - 1):
        _later(ctx)
        watch.record(ctx, line)

    [entry] = reflections.entries(ctx.places)
    assert "critic-ux a1b2c3d4 of s1" in entry["text"]


def test_an_entry_is_marked_fixed_and_opens_again_when_it_comes_back(ctx: Context) -> None:
    reflect(ctx, "friction", "The slot took four minutes to rebuild.")
    reflect(ctx, "miss", "A tester passed a menu that overflowed.")

    reflections.mark(ctx, {"ids": ["sr1", "sr2"], "state": "fixed", "note": "abc1234: a faster rebuild"}, None)
    assert isinstance(ctx.clock, FakeClock)
    fixed_at = ctx.clock.iso()
    ctx.clock.t += 600
    reflect(ctx, "friction", "The slot took four minutes to rebuild.")

    back, fixed = reflections.entries(ctx.places)
    assert (back["state"], back["closed"], back["came_back"], back["repeats"]) == ("open", None, True, 2)
    assert back["note"] == fixed["note"] == "abc1234: a faster rebuild"
    assert (fixed["state"], fixed["closed"]) == ("fixed", fixed_at)
    assert "sr2 · " in ctx.places.reflections.read_text() and "fixed: abc1234" in ctx.places.reflections.read_text()


@pytest.mark.parametrize(
    "args",
    [
        {"ids": ["sr1", "sr9"], "state": "fixed"},
        {"ids": [], "state": "fixed"},
        {"ids": ["sr1"], "state": "done"},
        {"ids": ["sr1"], "state": "ignored", "note": "x" * 5000},
    ],
    ids=["an-unknown-id", "no-id", "no-such-state", "a-note-too-long"],
)
def test_a_mark_with_a_bad_id_state_or_note_changes_nothing(ctx: Context, args: dict[str, Any]) -> None:
    reflect(ctx, "friction", "The slot took four minutes to rebuild.")
    before = reflections.entries(ctx.places)

    with pytest.raises(Refused):
        reflections.mark(ctx, args, None)
    assert reflections.entries(ctx.places) == before
