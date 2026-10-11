"""The owner's decisions: each posts its inbox event (R4); a reorder needs the order the device saw (R29)."""

from __future__ import annotations

import dataclasses

import pytest
from queue_kit import PICTURE, World

from runtime.queue import cards, formats, owner, proposals, tasks
from runtime.queue.book import task_rel
from runtime.queue.propose import propose
from runtime.shared.models import STOPPED, WAITING, WORKING
from runtime.shared.protocol import Refused
from runtime.warden import backstop, lifecycle, reflections, registry
from runtime.warden.backstop import Proc


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
def test_each_proposal_decision_posts_its_event(world: World, decide, event: str) -> None:  # type: ignore[no-untyped-def]
    task = world.task()
    propose(world.ctx(), task.id, 1, "why", "", PICTURE, "")
    decide(world, task.id)
    assert [kind for kind, _ in world.events][-2:] == ["proposal-slot-free", event]


def test_a_question_card_carries_the_answer_back_to_the_session_that_asked(world: World) -> None:
    task = world.task()
    asked = cards.needs_owner(world.ctx("designer", "s-design"), task.id, "", "", "Keep the old link?", PICTURE)
    owner.answer(world.owner(), asked.id, "Yes,\nkeep it")
    assert world.events[-1] == ("answered", {"card": asked.id, "task": task.id, "session": "s-design",
                                             "answer": "Yes, keep it"})  # fmt: skip
    with pytest.raises(Refused, match="already answered"):
        owner.answer(world.owner(), asked.id, "No")


def test_a_card_for_the_owner_needs_a_picture(world: World) -> None:
    task = world.task()
    with pytest.raises(Refused, match="picture"):
        cards.needs_owner(world.ctx("designer", "s-design"), task.id, "", "", "Keep the old link?", None)


def test_the_asking_session_waits_and_the_answer_reaches_it_once_and_it_works_again(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = world.task()
    world.session("s-impl", "implementer", "impl-t-0001", task.id)
    monkeypatch.setattr(backstop, "processes", lambda: {"s-impl": Proc(1, "s-impl", "impl-t-0001", "", "busy")})
    asked = cards.needs_owner(world.ctx("implementer", "s-impl"), task.id, "", "", "Keep the old link?", PICTURE)
    assert registry.read(world.places)["s-impl"].state == WAITING

    owner.answer(world.owner(), asked.id, "Yes")

    # Handed over as its next turn, its busy turn left alone; not relayed through the orchestrator as well.
    assert world.launcher.messages == [("s-impl", f"The owner answered your question on {task.id}: Yes")]
    assert world.launcher.commands == [] and world.events == []
    assert registry.read(world.places)["s-impl"].state == WORKING


def _stuck(world: World, monkeypatch: pytest.MonkeyPatch, status: str = "qa") -> str:
    """A running task whose implementer the orchestrator extended once; the task's id."""
    task = world.task()
    world.write_raw(task_rel(task.id), formats.dump_task(dataclasses.replace(task, status=status, run=1)))
    world.session("s-impl", "implementer", "impl-t-0001", task.id)
    monkeypatch.setattr(backstop, "processes", lambda: {"s-impl": Proc(1, "s-impl", "impl-t-0001", "", "idle")})
    lifecycle.extend(world.ctx(), {"session": "s-impl", "limit": "qa"}, None)
    return task.id


def test_a_limit_spent_again_after_one_extension_goes_to_the_owners_decide_card(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = _stuck(world, monkeypatch)

    with pytest.raises(Refused, match="the owner decides now"):
        lifecycle.extend(world.ctx(), {"session": "s-impl", "limit": "qa"}, None)

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

    owner.decide(world.owner(), card.id, "extend")

    assert registry.read(world.places)["s-impl"].extensions["qa"] == 2 * by
    assert [sid for sid, _ in world.launcher.messages] == ["s-impl"]
    assert world.events[-1][0] == "decided-extend" and world.queue().task(task_id).status == "building"

    for to, event, by_ in [("qa", "qa-ready", "implementer")]:
        tasks.move(world.ctx(), task_id, to, event, by_)
    cards.spent(world.ctx(), registry.read(world.places)["s-impl"], "qa", "spent again")
    again = next(c for c in cards.read(world.places) if c.kind == "decide" and not c.answered)
    owner.decide(world.owner(), again.id, "park")
    assert registry.read(world.places)["s-impl"].retired == STOPPED and world.launcher.stopped == ["s-impl"]
    assert world.queue().task(task_id).status == "parked"
