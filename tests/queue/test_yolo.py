"""YOLO mode: while it is on, the warden approves, answers and hands decisions to the orchestrator."""

from __future__ import annotations

import dataclasses

import pytest
from queue_kit import PICTURE, World

from runtime.queue import cards, formats, proposals
from runtime.queue.book import task_rel
from runtime.queue.propose import propose
from runtime.shared.models import DEPRIORITIZED
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


def _benched(world: World, sid: str, status: str) -> None:
    task = world.task()
    world.write_raw(task_rel(task.id), formats.dump_task(dataclasses.replace(task, status=status, run=1)))
    world.session(sid, "implementer", f"impl-{sid}", task.id)
    sessions = registry.read(world.places)
    sessions[sid] = dataclasses.replace(sessions[sid], state=DEPRIORITIZED, benched_for=task.id)
    registry.write(world.places, sessions)


def test_yolo_approves_only_into_a_free_place_of_the_runs_own_role(world: World) -> None:
    world.with_settings(caps__implementers=3, caps__designers=1, routing__max_proposals=4)
    builds = [world.task().id for _ in range(2)]
    designs = [world.task().id for _ in range(2)]
    for task_id in builds:
        propose(world.ctx(), task_id, 1, "why", "", PICTURE, "")
    for task_id in designs:
        propose(world.ctx(), task_id, 3, "why", "it needs a design", PICTURE, "")
    world.session("s-impl", "implementer", "impl-x", world.task().id, "/wt/x")
    _benched(world, "s-pushed-out", "deprioritized")  # holds its place to come back to
    _benched(world, "s-merged", "merged")  # holds none: it waits on its check
    yolo.run_pass(world.ctx())
    assert proposals.read(world.places).approved == []

    _switch(world, True)
    yolo.run_pass(world.ctx())
    yolo.run_pass(world.ctx())

    approved = proposals.read(world.places).approved
    assert sorted(approved) == sorted([builds[0], designs[0]])
    notes = [e["text"] for e in world.queue().read_log(approved[0])]
    assert any(note.endswith(", by YOLO mode") for note in notes)


def test_yolo_approves_again_a_task_whose_own_run_holds_its_place_though_no_other_place_is_free(
    world: World,
) -> None:
    world.with_settings(caps__implementers=1, routing__max_proposals=4)
    held, other = world.task().id, world.task().id
    world.session("s-impl", "implementer", f"impl-{held}", held, "/wt/x")
    for task_id in (other, held):
        propose(world.ctx(), task_id, 1, "why", "", PICTURE, "")
    _switch(world, True)

    yolo.run_pass(world.ctx())

    assert world.queue().task(held).status == "building" and world.queue().task(other).status == "proposed"


def test_yolo_answers_a_question_asked_of_the_owner_as_the_owner(world: World) -> None:
    asked = cards.needs_owner(world.ctx("designer", "s-design"), world.task().id, "", "Keep the link?", PICTURE)
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
