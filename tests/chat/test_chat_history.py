"""`factory chat <request>`: an agent reads the talk around a request from the chat through the warden, a page at a
time, as data, and only so far."""

from __future__ import annotations

from typing import Any

import pytest
from chat_kit import ALICE, BRUNO, CHANNEL, OTHER, TOKEN, FakeDiscord
from queue_kit import World

from runtime.chat import history, pull
from runtime.queue import reports
from runtime.shared.models import Request
from runtime.shared.protocol import Refused


def _talk(world: World, chat: FakeDiscord) -> tuple[Request, list[str]]:
    """A request filed from a tag that replies to the first of five messages; the five messages' ids."""
    pull.poll(world.ctx("warden", session=""))
    said = [chat.say(CHANNEL, ALICE, f"message {n}") for n in range(4)]
    said.append(chat.say(CHANNEL, BRUNO, "#factory yes, this​", replies_to=said[0]))
    world.clock.at += world.settings.get_int("chat.poll_minutes") * 60
    pull.poll(world.ctx("warden", session=""))
    [req] = [r for r in world.queue().requests()[0] if r.inbox == "chat"]
    return req, said


def _read(world: World, **args: Any) -> dict[str, Any]:
    return history.read(world.ctx("orchestrator", agent_id="atriage0000001", agent_kind="triage"), args, None)


def test_an_agent_reads_the_talk_around_the_tagged_message_as_data_and_pages_back(
    world: World, chat: FakeDiscord
) -> None:
    world.with_settings(chat__page_messages=2)
    req, said = _talk(world, chat)

    around = _read(world, request=req.id)
    assert (around["tagged"], around["notice"], [m["id"] for m in around["messages"]]) == (
        said[4],
        reports.NOTICE,
        said[3:5],
    )
    tagged = around["messages"][1]
    assert (tagged["from"], tagged["text"], tagged["replies_to"]) == (BRUNO, "#factory yes, this<U+200B>", said[0])
    assert [m["text"] for m in around["replied_to"]] == ["message 0"]
    before = _read(world, request=req.id, before=said[3])
    assert ([m["id"] for m in before["messages"]], before["pages_left"]) == (said[1:3], 8)
    assert TOKEN not in str(before)


def test_one_requests_agents_read_at_most_read_pages_and_a_page_discord_fails_to_give_counts_not(
    world: World, chat: FakeDiscord
) -> None:
    world.with_settings(chat__read_pages=2)
    req, _ = _talk(world, chat)
    chat.status = {"GET": 502}
    with pytest.raises(Refused, match="HTTP 502"):
        _read(world, request=req.id)
    chat.status = {}

    assert [_read(world, request=req.id)["pages_left"] for _ in range(2)] == [1, 0]
    with pytest.raises(Refused) as refused:
        _read(world, request=req.id)
    assert refused.value.rule == "chat-pages"


@pytest.mark.parametrize(
    ("change", "rule"),
    [
        ("not-from-the-chat", "args"),
        ("no-such-request", "request"),
        ("two-places", "args"),
        ("not-a-message-id", "args"),
        ("channel-no-longer-read", "chat"),
    ],
)
def test_a_read_the_factory_cannot_place_is_refused_and_reads_nothing(
    world: World, chat: FakeDiscord, change: str, rule: str
) -> None:
    req, said = _talk(world, chat)
    asked = len(chat.asked)
    args: dict[str, Any] = {
        "not-from-the-chat": {"request": world.request().id},
        "no-such-request": {"request": "r-2026-10-09-0999"},
        "two-places": {"request": req.id, "before": said[1], "after": said[2]},
        "not-a-message-id": {"request": req.id, "before": "../../users/@me"},
    }.get(change, {"request": req.id})
    if change == "channel-no-longer-read":
        world.with_settings(chat__channels=[OTHER])
    with pytest.raises(Refused) as refused:
        _read(world, **args)

    assert refused.value.rule == rule and len(chat.asked) == asked
