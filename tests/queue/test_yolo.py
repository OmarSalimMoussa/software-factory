"""YOLO mode: while it is on, the warden approves, answers and hands decisions to the orchestrator."""

from __future__ import annotations

import dataclasses

import pytest
from queue_kit import PICTURE, World

from runtime.queue import cards, formats, proposals
from runtime.queue.book import task_rel
from runtime.queue.propose import propose
from runtime.shared.protocol import Refused
from runtime.warden import inbox
from runtime.warden.owner import yolo
from runtime.warden.sessions import backstop, limits, registry
from runtime.warden.sessions.backstop import Proc


def _switch(world: World, on: bool) -> None:
    yolo.owner_switch(world.owner(), {"on": on}, None)


def test_switching_yolo_tells_the_orchestrator_and_switching_it_again_changes_nothing(world: World) -> None:
    _switch(world, True)
    _switch(world, True)
    _switch(world, False)

    assert [kind for kind, _ in world.events] == [inbox.YOLO_ON, inbox.YOLO_OFF] and yolo.state(world.places) is None


def test_yolo_approves_proposals_while_fewer_runs_wait_than_the_factory_runs_at_once(world: World) -> None:
    world.with_settings(caps__implementers=1)
    for _ in range(2):
        propose(world.ctx(), world.task().id, 1, "why", "", PICTURE, "")
    yolo.run_pass(world.ctx())
    assert proposals.read(world.places).approved == []

    _switch(world, True)
    yolo.run_pass(world.ctx())
    yolo.run_pass(world.ctx())

    found = proposals.read(world.places)
    assert len(found.approved) == 1 and len([e for e in found.open if e and e != "none"]) == 1
    notes = [e["text"] for e in world.queue().read_log(found.approved[0])]
    assert any(note.endswith(", by YOLO mode") for note in notes)


def test_yolo_answers_a_question_asked_of_the_owner_as_the_owner(world: World) -> None:
    asked = cards.needs_owner(world.ctx("designer", "s-design"), world.task().id, "", "", "Keep the link?", PICTURE)
    _switch(world, True)

    yolo.run_pass(world.ctx())

    assert next(c for c in cards.read(world.places) if c.id == asked.id).answered == yolo.ANSWER


def test_yolo_hands_each_decision_to_the_orchestrator_once_and_only_then_may_it_decide(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = world.task()
    world.write_raw(task_rel(task.id), formats.dump_task(dataclasses.replace(task, status="qa", run=1)))
    world.session("s-impl", "implementer", "impl-t-0001", task.id)
    monkeypatch.setattr(backstop, "processes", lambda: {"s-impl": Proc(1, "s-impl", "impl-t-0001", "", "idle")})
    limits.extend(world.ctx(), {"session": "s-impl", "limit": "qa"}, None)
    cards.spent(world.ctx(), registry.read(world.places)["s-impl"], "qa", "spent again")
    card = next(c for c in cards.read(world.places) if c.kind == "decide")
    with pytest.raises(Refused) as off:
        yolo.orchestrator_decide(world.ctx(), {"card": card.id, "choice": "park"}, None)
    assert off.value.rule == "yolo"

    _switch(world, True)
    yolo.run_pass(world.ctx())
    yolo.run_pass(world.ctx())

    handed = [data for kind, data in world.events if kind == inbox.YOURS]
    assert handed == [{"card": card.id, "kind": "decide", "task": task.id, "choices": ["extend", "designer", "park"]}]
    with pytest.raises(Refused) as wrong:
        yolo.orchestrator_decide(world.ctx(), {"card": card.id, "choice": "go"}, None)
    assert wrong.value.rule == "choice"
    yolo.orchestrator_decide(world.ctx(), {"card": card.id, "choice": "park"}, None)
    assert world.queue().task(task.id).status == "parked"
