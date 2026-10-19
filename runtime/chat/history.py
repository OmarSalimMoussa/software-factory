"""`factory chat <request>`: the talk around a request from the chat, a page at a time, for the agents that triage
it or take it in. The warden calls Discord with the bot's token; the agent gets the messages alone, as data, behind
the reports' notice, with every hidden character shown.

With no position, the page is around the tagged message; `--before`, `--after` or `--around <message id>` moves it.
Each message names the one it replies to, and a replied-to message the page does not hold comes along when Discord
sends it. Only a channel still in chat.channels is read, and one request's agents read at most chat.read_pages pages;
a page Discord fails to give is not counted.
"""

from __future__ import annotations

from typing import Any

from runtime.chat import discord, pull
from runtime.chat.discord import Message
from runtime.queue.book import Queue
from runtime.queue.reports import NOTICE, shown
from runtime.shared import store
from runtime.shared.models import CHAT, CHAT_ID, CHAT_PAGES, Request
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused, ok
from runtime.warden.context import Context
from runtime.warden.health import telemetry


def _reads(places: Places) -> dict[str, int]:
    raw = store.read_json_or(places.state / "chat-reads.json", {})
    if not isinstance(raw, dict) or not all(isinstance(pages, int) for pages in raw.values()):
        raise store.StoreError(f"{places.state / 'chat-reads.json'}: not an object of counts")
    return raw


def _count(ctx: Context, request: str, by: int) -> int:
    """The pages read for ``request`` once ``by`` more count; refused past chat.read_pages."""
    most = ctx.settings().get_int("chat.read_pages")
    with ctx.lock:
        reads = _reads(ctx.places)
        pages = reads.get(request, 0) + by
        if pages > most:
            raise Refused("chat-pages", f"{request} has read its {most} pages of the chat: work from what they hold")
        reads[request] = pages
        store.write_json(ctx.places.state / "chat-reads.json", reads, mode=0o600)
    return pages


def _request(ctx: Context, given: object) -> Request:
    if not isinstance(given, str):
        raise Refused("args", "name the request: factory chat <request id>")
    try:
        req = Queue.of(ctx).request(given)
    except FileNotFoundError as exc:
        raise Refused("request", f"there is no request {given}") from exc
    if req.inbox != CHAT:
        raise Refused("args", f"{given} is not from the chat: only a request from the chat has talk around it")
    return req


def _place(args: dict[str, Any], tagged: str) -> tuple[str, str]:
    given = [position for position in CHAT_PAGES if args.get(position) is not None]
    if len(given) > 1:
        raise Refused("args", "read before, after or around one message, not more")
    position, ref = (given[0], args[given[0]]) if given else ("around", tagged)
    if not isinstance(ref, str) or not CHAT_ID.fullmatch(ref):
        raise Refused("args", f"--{position} names a message by its id, as a page lists them")
    return position, ref


def _row(message: Message) -> dict[str, Any]:
    return {
        "id": message.id,
        "from": message.author,
        "name": None if message.name is None else shown(message.name),
        "bot": message.bot,
        "at": message.at,
        "text": shown(message.text),
        "replies_to": None if message.replies_to is None else message.replies_to.id,
    }


def read(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    settings = ctx.settings()
    req = _request(ctx, args.get("request"))
    position, ref = _place(args, req.report)
    record = pull.read(ctx.places)["filed"].get(req.report)
    if record is None or record["channel"] not in settings.get_list("chat.channels"):
        raise Refused("chat", f"the factory reads no channel {req.id} was said in")
    pages = _count(ctx, req.id, 1)
    try:
        secret, timeout = discord.token(ctx.places), float(settings.get_int("chat.timeout_seconds"))
        page = discord.messages(
            secret, record["channel"], position, ref, settings.get_int("chat.page_messages"), timeout
        )
    except discord.ChatError as exc:
        _count(ctx, req.id, -1)
        raise Refused("chat", str(exc)) from exc
    held = {message.id for message in page}
    replied = {m.replies_to.id: _row(m.replies_to) for m in page if m.replies_to and m.replies_to.id not in held}
    answer = ok(
        request=req.id,
        tagged=req.report,
        notice=NOTICE,
        messages=[_row(message) for message in page],
        replied_to=list(replied.values()),
        pages_left=settings.get_int("chat.read_pages") - pages,
    )
    return telemetry.fit(answer, settings.get_int("chat.max_kib") * 1024)
