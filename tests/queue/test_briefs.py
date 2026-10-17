"""Briefs on this machine: a designer saves versions and answers comments, the owner reads and comments, and a spec's
sign-off names the version it was asked on."""

from __future__ import annotations

import base64
import dataclasses

import pytest
from queue_kit import PICTURE, World

from runtime.queue import cards, documents
from runtime.shared.models import WAITING, WORKING
from runtime.shared.protocol import Refused
from runtime.warden.context import Context
from runtime.warden.owner import briefs
from runtime.warden.sessions import registry

PAGE = b"<!doctype html><title>Done mark</title><body><p>The check draws itself in 300 ms.</p></body>"


def _designer(world: World) -> tuple[str, Context]:
    task = world.task()
    world.session("s-design", "designer", "design-x", task=task.id)
    return task.id, world.ctx("designer", "s-design")


def test_a_brief_keeps_every_version_and_the_owner_reads_any_of_them(world: World) -> None:
    task, ctx = _designer(world)

    assert briefs.save(ctx, {"task": task}, PAGE)["version"] == 1
    assert briefs.save(ctx, {"task": task}, PAGE.replace(b"300", b"180"))["version"] == 2

    first = briefs.page(world.owner(), {"task": task, "version": 1}, None)
    assert base64.b64decode(first["bytes_b64"]) == PAGE
    assert [v["n"] for v in briefs.read(world.places, task)["versions"]] == [1, 2]


@pytest.mark.parametrize(
    ("who", "page", "refusal"),
    [("other", PAGE, "only the designer of"), ("designer", b"", "name the brief's HTML file"),
     ("designer", b"\xff\xfe", "not UTF-8")],
    ids=["another-session", "empty", "not-text"],
)  # fmt: skip
def test_only_its_designer_saves_a_brief_and_only_as_text(world: World, who: str, page: bytes, refusal: str) -> None:
    task, ctx = _designer(world)
    world.session("s-other", "designer", "design-y", task=world.task().id)
    caller = ctx if who == "designer" else world.ctx("designer", "s-other")
    with pytest.raises(Refused, match=refusal):
        briefs.save(caller, {"task": task}, page)


def test_a_comment_waits_for_the_designer_then_its_answer_waits_for_the_owner(world: World) -> None:
    task, ctx = _designer(world)
    briefs.save(ctx, {"task": task}, PAGE)
    asked = {"task": task, "version": 1, "quote": "300 ms", "prefix": "draws itself in ", "suffix": ".",
             "note": "Feels slow."}  # fmt: skip
    thread = briefs.comment(world.owner(), asked, None)["thread"]
    assert briefs.open_threads(world.places, task) == 1

    read = briefs.comments(ctx, {"task": task}, None)
    assert [(t["quote"], t["note"], t["state"]) for t in read["threads"]] == [("300 ms", "Feels slow.", "open")]
    briefs.reply(ctx, {"task": task, "thread": thread, "text": "Cut to 180 ms."}, None)
    assert briefs.open_threads(world.places, task) == 0

    briefs.resolve(world.owner(), {"task": task, "thread": thread}, None)
    assert briefs.comments(ctx, {"task": task}, None)["threads"] == []
    with pytest.raises(Refused, match="name a saved version"):
        briefs.comment(world.owner(), {**asked, "version": 3}, None)


@pytest.mark.parametrize("asked", [True, False], ids=["the-owners-card-is-open", "no-card-yet"])
def test_a_spec_revised_while_the_owners_card_is_open_sets_its_designer_waiting_again(
    world: World, asked: bool
) -> None:
    task, ctx = _designer(world)
    world.queue().write_task(dataclasses.replace(world.queue().task(task), status="designing", run=4))
    documents.save_spec(ctx, task, b"# A spec\n\nThe plan.\n", 2)
    briefs.save(ctx, {"task": task}, PAGE)
    if asked:
        cards.needs_owner(ctx, task, "Go?", "", PICTURE)
    sessions = registry.read(world.places)  # the owner's feedback set it working on the revision
    sessions["s-design"] = dataclasses.replace(sessions["s-design"], state=WORKING)
    registry.write(world.places, sessions)

    documents.save_spec(ctx, task, b"# A spec\n\nThe revised plan.\n", 2)

    assert registry.read(world.places)["s-design"].state == (WAITING if asked else WORKING)


def test_a_specs_sign_off_names_the_brief_version_it_asks_on(world: World) -> None:
    task, ctx = _designer(world)
    world.queue().write_task(dataclasses.replace(world.queue().task(task), status="designing", run=4))
    documents.save_spec(ctx, task, b"# A spec\n\nThe plan.\n", 2)
    with pytest.raises(Refused, match="save the brief"):
        cards.needs_owner(ctx, task, "Go?", "", PICTURE)
    briefs.save(ctx, {"task": task}, PAGE)
    briefs.save(ctx, {"task": task}, PAGE)

    card = cards.needs_owner(ctx, task, "Go?", "", PICTURE)

    assert (card.kind, card.brief) == ("spec", "v2")
