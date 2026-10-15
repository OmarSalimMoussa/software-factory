"""The owner's decisions: each posts its inbox event (R4); a reorder needs the order the device saw (R29)."""

from __future__ import annotations

import dataclasses
import threading
from collections.abc import Callable

import pytest
from queue_kit import PICTURE, World

from runtime.queue import cards, formats, owner, proposals, turn
from runtime.queue.book import task_rel
from runtime.queue.propose import propose
from runtime.shared import store
from runtime.shared.models import RETIRED, STOPPED, WAITING, WORKING
from runtime.shared.protocol import Refused
from runtime.warden.owner import ops, reflections
from runtime.warden.sessions import backstop, limits, registry
from runtime.warden.sessions.backstop import Proc


def test_a_reorder_from_a_stale_page_is_refused(world: World) -> None:
    first, second = world.task(), world.task()
    for task in (first, second):
        propose(world.ctx(), task.id, 1, "why", "", PICTURE, "")
    with pytest.raises(Refused) as refused:
        owner.move(world.owner(), second.id, "up", [second.id, first.id], approved=False)
    assert refused.value.rule == "stale"
    owner.move(world.owner(), second.id, "up", [first.id, second.id], approved=False)
    assert proposals.read(world.places).open == [second.id, first.id]


@pytest.mark.parametrize(
    ("decide", "event"),
    [
        (lambda w, t: owner.later(w.owner(), t), "not-now"),
        (lambda w, t: owner.drop(w.owner(), t, "not worth it"), "dropped"),
        (lambda w, t: owner.approve(w.owner(), t), "approved"),
    ],
)
def test_each_proposal_decision_posts_its_event(
    world: World, decide: Callable[[World, str], object], event: str
) -> None:
    task = world.task()
    propose(world.ctx(), task.id, 1, "why", "", PICTURE, "")
    decide(world, task.id)
    assert [kind for kind, _ in world.events][-2:] == ["proposal-slot-free", event]


def test_a_question_card_carries_the_answer_back_to_the_session_that_asked(world: World) -> None:
    task = world.task()
    asked = cards.needs_owner(world.ctx("designer", "s-design"), task.id, "", "Keep the old link?", PICTURE)
    owner.answer(world.owner(), asked.id, "Yes,\nkeep it")
    assert world.events[-1] == ("answered", {"card": asked.id, "task": task.id, "session": "s-design",
                                             "answer": "Yes, keep it"})  # fmt: skip
    with pytest.raises(Refused, match="already answered"):
        owner.answer(world.owner(), asked.id, "No")


def test_a_card_for_the_owner_needs_a_picture(world: World) -> None:
    task = world.task()
    with pytest.raises(Refused, match="picture"):
        cards.needs_owner(world.ctx("designer", "s-design"), task.id, "", "Keep the old link?", None)


@pytest.mark.parametrize("now", ["s-impl", "s-copy"], ids=["as-it-asked", "come-back-as-a-copy"])
def test_the_asking_session_waits_and_the_answer_reaches_it_once_and_it_works_again(
    world: World, monkeypatch: pytest.MonkeyPatch, now: str
) -> None:
    task = world.task()
    world.session("s-impl", "implementer", "impl-t-0001", task.id)
    asked = cards.needs_owner(world.ctx("implementer", "s-impl"), task.id, "", "Keep the old link?", PICTURE)
    if now != "s-impl":  # it came back as a copy while it waited: its row moved, and the history says where
        sessions = registry.read(world.places)
        sessions[now] = dataclasses.replace(sessions.pop("s-impl"), id=now)
        registry.write(world.places, sessions)
        store.append_jsonl(world.places.history, {"at": "2026-10-09T12:00:00Z", "session": now, "was": "s-impl",
                                                  "name": "impl-t-0001", "by": "warden", "why": "renewed"})  # fmt: skip
    monkeypatch.setattr(backstop, "processes", lambda: {now: Proc(1, now, "impl-t-0001", "", "busy")})
    assert registry.read(world.places)[now].state == WAITING

    let_go, heard = threading.Event(), world.launcher.message_session
    monkeypatch.setattr(world.launcher, "message_session", lambda sid, text: let_go.wait(10) and heard(sid, text))

    answer = owner.answer(world.owner(), asked.id, "Yes")

    # Recorded and answered at once: handing it over takes longer than the dashboard waits.
    assert (
        world.launcher.messages == []
        and next(c for c in cards.read(world.places) if c.id == asked.id).answered == "Yes"
    )
    let_go.set()
    ops.join(answer["op"], 10)
    # Handed over as its next turn, its busy turn left alone; not relayed through the orchestrator as well.
    assert world.launcher.messages == [(now, f"The owner answered your question on {task.id}: Yes")]
    assert world.launcher.commands == [] and world.events == []
    assert registry.read(world.places)[now].state == WORKING


def _stuck(world: World, monkeypatch: pytest.MonkeyPatch, status: str = "qa") -> str:
    """A running task whose implementer the orchestrator extended once; the task's id."""
    task = world.task()
    world.write_raw(task_rel(task.id), formats.dump_task(dataclasses.replace(task, status=status, run=1)))
    world.session("s-impl", "implementer", "impl-t-0001", task.id)
    monkeypatch.setattr(backstop, "processes", lambda: {"s-impl": Proc(1, "s-impl", "impl-t-0001", "", "idle")})
    limits.extend(world.ctx(), {"session": "s-impl", "limit": "qa"}, None)
    return task.id


def test_a_limit_spent_again_after_one_extension_goes_to_the_owners_decide_card(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = _stuck(world, monkeypatch)

    with pytest.raises(Refused, match="the owner decides now"):
        limits.extend(world.ctx(), {"session": "s-impl", "limit": "qa"}, None)

    card = next(c for c in cards.read(world.places) if c.kind == "decide")
    assert (card.task, card.limit, world.queue().task(task_id).status) == (task_id, "qa", "decide")


def test_the_guards_refusal_of_a_round_spent_after_one_extension_puts_the_task_on_the_card(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = _stuck(world, monkeypatch)
    refused = {"session": "s-impl", "rule": "qa-rounds", "tool": "Agent", "detail": "this would be QA round 7"}

    reflections.RefusalWatch().record(world.ctx(), refused)

    assert world.queue().task(task_id).status == "decide"


def test_the_owners_extend_raises_the_live_sessions_limit_and_tells_it_and_park_ends_it(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = _stuck(world, monkeypatch)
    cards.spent(world.ctx(), registry.read(world.places)["s-impl"], "qa", "spent again")
    card = next(c for c in cards.read(world.places) if c.kind == "decide")
    by = world.settings.get_int("rounds.extend_by")

    ops.join(owner.decide(world.owner(), card.id, "extend")["op"])

    assert registry.read(world.places)["s-impl"].extensions["qa"] == 2 * by
    assert [sid for sid, _ in world.launcher.messages] == ["s-impl"]
    assert world.events[-1][0] == "decided-extend" and world.queue().task(task_id).status == "qa"  # where it stopped

    cards.spent(world.ctx(), registry.read(world.places)["s-impl"], "qa", "spent again")
    again = next(c for c in cards.read(world.places) if c.kind == "decide" and not c.answered)
    owner.decide(world.owner(), again.id, "park")
    assert registry.read(world.places)["s-impl"].retired == STOPPED and world.launcher.stopped == ["s-impl"]
    assert world.queue().task(task_id).status == "parked"


def test_the_owners_extend_of_a_task_stopped_in_its_merge_turn_sends_it_back_for_its_next_turn(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = _stuck(world, monkeypatch, status="turn")
    cards.spent(world.ctx(), registry.read(world.places)["s-impl"], "qa", "spent again")
    card = next(c for c in cards.read(world.places) if c.kind == "decide")
    other = world.task(branch="impl-b")  # takes the turn while the owner decides
    world.write_raw(task_rel(other.id), formats.dump_task(dataclasses.replace(other, status="qa-clean", run=1)))
    world.launcher.git = lambda argv: (0, "c0ffee" * 6 + "abcd\n")
    assert turn.next_turn(world.ctx(), {}, None)["task"] == other.id

    ops.join(owner.decide(world.owner(), card.id, "extend")["op"])

    assert world.queue().task(task_id).status == "qa-clean"
    assert turn.holder(world.places) == {"task": other.id, "branch": "impl-b", "base": "c0ffee" * 6 + "abcd"}
    assert "Your merge turn ended while you waited" in world.launcher.messages[-1][1]


def test_the_owners_extend_of_a_designers_spent_limit_sends_the_task_back_to_its_designer(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = world.task()
    world.write_raw(task_rel(task.id), formats.dump_task(dataclasses.replace(task, status="designing", run=3)))
    world.session("s-design", "designer", "design-t-0001", task.id)
    procs = {"s-design": Proc(1, "s-design", "design-t-0001", "", "idle")}
    monkeypatch.setattr(backstop, "processes", lambda: procs)
    limits.extend(world.ctx(), {"session": "s-design", "limit": "critic"}, None)
    cards.spent(world.ctx(), registry.read(world.places)["s-design"], "critic", "spent again")
    card = next(c for c in cards.read(world.places) if c.kind == "decide")

    ops.join(owner.decide(world.owner(), card.id, "extend")["op"])

    assert world.queue().task(task.id).status == "designing"
    assert registry.read(world.places)["s-design"].state != RETIRED and world.launcher.stopped == []
