"""`factory propose` (routing spec, R4, R21): the place limit, the picture, the why and the reason for another run."""

from __future__ import annotations

import dataclasses

import pytest
from queue_kit import PICTURE, World

from runtime.queue import formats, owner, pictures, proposals, tasks
from runtime.queue.book import task_rel
from runtime.queue.propose import propose, propose_none
from runtime.shared.protocol import Refused
from runtime.warden.sessions import backstop, registry
from runtime.warden.sessions.backstop import Proc

PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 17


def _propose(world: World, task_id: str, run: int = 1, reason: str = "", picture: bytes | None = PICTURE) -> None:
    propose(world.ctx(), task_id, run, "every new user meets it", reason, picture, "")


def test_no_more_proposals_than_places(world: World) -> None:
    world.with_settings(routing__max_proposals=2)
    first, second, third = world.task(), world.task(), world.task()
    _propose(world, first.id)
    _propose(world, second.id)
    with pytest.raises(Refused) as refused:
        _propose(world, third.id)
    assert refused.value.rule == "max-proposals"
    assert world.queue().task(third.id).status == "queued"
    owner.later(world.owner(), first.id)
    _propose(world, third.id)
    assert proposals.read(world.places).open == [third.id, second.id]


@pytest.mark.parametrize("why", ["boosted", "severe"])
def test_a_task_the_owner_boosted_or_a_severe_one_goes_first_even_when_every_place_is_held(
    world: World, why: str
) -> None:
    world.with_settings(routing__max_proposals=2)
    first, second = world.task(), world.task()
    urgent = world.task(severity="severe") if why == "severe" else world.task()
    _propose(world, first.id)
    _propose(world, second.id)
    if why == "boosted":
        owner.boost(world.owner(), urgent.id, on=True)
    _propose(world, urgent.id)
    assert proposals.read(world.places).open == [urgent.id, first.id, second.id]


@pytest.mark.parametrize(
    "picture",
    [
        None,
        b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg"><foreignObject><p>x</p></foreignObject></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>',
        b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:x="http://www.w3.org/1999/xlink">'
        b'<use x:href="https://evil.example/a.svg#b"/></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg"><rect style="fill:url(https://evil.example/x)"/></svg>',
        b'<!DOCTYPE svg [<!ENTITY a "aaaa">]><svg xmlns="http://www.w3.org/2000/svg">&a;</svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg"><style>@import url(https://evil.example)</style></svg>',
        b"<html><body>not a picture</body></html>",
        b"\x89PNG\r\n\x1a\nnot really",
    ],
)
def test_a_proposal_needs_a_plain_picture(world: World, picture: bytes | None) -> None:
    task = world.task()
    with pytest.raises(Refused) as refused:
        _propose(world, task.id, picture=picture)
    assert refused.value.rule == "picture"
    assert world.queue().task(task.id).status == "queued"
    assert proposals.read(world.places).open == []


def test_a_clean_picture_is_kept_without_its_comments_and_named_by_its_content(world: World) -> None:
    data, kind = pictures.clean(PICTURE.replace(b"<rect", b"<!-- note --><rect"), 512)
    assert kind == "svg" and b"note" not in data and b"<rect" in data
    assert pictures.clean(PNG, 512) == (PNG, "png")
    with pytest.raises(Refused, match="larger"):
        pictures.clean(PNG + b"\x00" * 2048, 1)


def test_a_run_other_than_the_route_needs_a_reason(world: World) -> None:
    task = world.task()  # its facts route it to rung 1
    with pytest.raises(Refused, match="needs --reason"):
        _propose(world, task.id, run=2)
    _propose(world, task.id, run=2, reason="the cause is shaky")
    proposed = world.queue().task(task.id)
    assert (proposed.status, proposed.run) == ("proposed", 2)
    assert "the cause is shaky" in world.queue().read_log(task.id)[-1]["text"]


def test_a_proposal_whose_facts_route_it_elsewhere_goes_back_to_be_proposed_on_them(world: World) -> None:
    task = world.task()  # its facts route it to rung 1, so it is proposed with no reason
    _propose(world, task.id)
    tasks.set_facts(world.ctx(), task.id, {"cause": ""})  # an unconfirmed cause routes it higher
    back = world.queue().task(task.id)
    assert back.status == "queued" and task.id not in proposals.read(world.places).open
    assert "propose it again" in world.queue().read_log(task.id)[-1]["text"]
    with pytest.raises(Refused, match="needs --reason"):
        _propose(world, task.id)  # rung 1 is now below its route


def test_facts_on_a_queued_task_that_cannot_be_routed_yet_are_kept(world: World) -> None:
    task = world.task(slices=None)  # no slice estimate yet, so no route
    tasks.set_facts(world.ctx(), task.id, {"cause": "web/auth.py:52"})
    assert world.queue().task(task.id).cause == "web/auth.py:52"


def test_a_proposal_says_why_in_one_line(world: World) -> None:
    task = world.task()
    with pytest.raises(Refused, match="why"):
        propose(world.ctx(), task.id, 1, "", "", PICTURE, "")


def test_nothing_else_fills_every_free_place_until_a_proposal_takes_one(world: World) -> None:
    task = world.task()
    _propose(world, task.id)
    propose_none(world.ctx())
    assert proposals.read(world.places).open == [task.id, proposals.NONE, proposals.NONE]
    _propose(world, world.task().id)
    assert proposals.read(world.places).open[1] not in (None, proposals.NONE)


def test_a_task_the_owner_said_not_now_to_waits_for_a_change(world: World) -> None:
    task = world.task()
    _propose(world, task.id)
    owner.later(world.owner(), task.id)
    with pytest.raises(Refused, match="not now"):
        _propose(world, task.id)
    owner.boost(world.owner(), task.id, on=True)
    _propose(world, task.id)


def test_a_run_moved_up_a_rung_stops_its_running_session(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    task = world.task()
    world.write_raw(task_rel(task.id), formats.dump_task(dataclasses.replace(task, status="building", run=1)))
    world.session("s-impl", "implementer", "impl-t-0001", task.id)
    monkeypatch.setattr(backstop, "processes", lambda: {"s-impl": Proc(1, "s-impl", "impl-t-0001", "", "idle")})

    propose(world.ctx(), task.id, 2, "it needs the larger model", "it touches billing", PICTURE, "")

    row = registry.read(world.places)["s-impl"]
    assert (row.state, row.retired, world.launcher.stopped) == ("retired", "stopped", ["s-impl"])
    assert world.queue().task(task.id).status == "proposed"
