"""The owner's decisions: each posts its inbox event (R4); a reorder needs the order the device saw (R29)."""

from __future__ import annotations

import pytest
from queue_kit import PICTURE, World

from runtime.queue import cards, owner, proposals
from runtime.queue.propose import propose
from runtime.shared.models import WAITING
from runtime.shared.protocol import Refused
from runtime.warden import lifecycle, registry


def test_a_reorder_from_a_stale_page_is_refused(world: World) -> None:
    first, second = world.task(), world.task()
    for task in (first, second):
        propose(world.ctx(), task.id, 1, "why", "", PICTURE)
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
    propose(world.ctx(), task.id, 1, "why", "", PICTURE)
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


def test_the_asking_session_waits_and_the_answer_resumes_it(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    task = world.task()
    world.session("s-impl", "implementer", "impl-t-0001", task.id)
    woken: list[tuple[str, str]] = []
    monkeypatch.setattr(lifecycle, "wake", lambda ctx, s, message: woken.append((s.id, message)))
    asked = cards.needs_owner(world.ctx("implementer", "s-impl"), task.id, "", "", "Keep the old link?", PICTURE)
    assert registry.read(world.places)["s-impl"].state == WAITING
    owner.answer(world.owner(), asked.id, "Yes")
    assert woken == [("s-impl", f"The owner answered your question on {task.id}: Yes")]
