"""The chat pass: a message tagged for the factory in a chat channel becomes one request in the chat inbox, and the
factory answers on that message as the bot.

Every chat.poll_minutes, in a thread off the tick, the warden reads what came in each channel of chat.channels after
its cursor; a channel read for the first time starts from that moment, so nothing said before is filed. A message
holding chat.tag as a whole word, in any case, from anyone (or only from chat.senders, when it names some), is filed
once, as a user report is: its words, and those of the message it replies to, are kept quoted with every hidden
character shown, behind the reports' notice, in queue/chat/, and the request points at them. Triage takes it at once
(runtime/queue/triage.py). The factory skips exactly the messages it posted, by their ids: other agents post as the
same bot, and their tags count.

On a filed message the factory reacts and replies in fixed words: received, then what triage made of the request,
again whenever that changes. The only free text is triage's own title or reason, on one line, without links,
mentions or the tag, and a reply mentions no one. A reply Discord refuses for that message alone is logged and given
up; any other failure fails the pass, which runs again at the next poll and is reported as the errors import's is.
State: chat.json holds each channel's cursor, each message filed with its request and the answers given, the ids of
the factory's own posts, and until when Discord asked it to wait, beside the timer runtime/warden/imports.py keeps.
"""

from __future__ import annotations

import copy
import hashlib
import logging
import re
from pathlib import Path
from typing import Any

from runtime.chat import discord
from runtime.chat.discord import Message
from runtime.queue import formats, intake
from runtime.queue.book import Queue
from runtime.queue.reports import NOTICE, quoted
from runtime.shared import store
from runtime.shared.models import CHAT
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused
from runtime.shared.settings import Settings
from runtime.warden import imports
from runtime.warden.context import Context

log = logging.getLogger(__name__)

TIMED = imports.Timed(
    "chat import",
    "reading the chat",
    "chat.poll_minutes",
    expected=(discord.ChatError, OSError, store.StoreError, Refused),
    told=(discord.ChatError, Refused),
)
_FRESH: dict[str, Any] = {"cursors": {}, "filed": {}, "posted": [], "wait_until": None, **imports.fresh()}
# A Discord id counts the milliseconds since 2015 began, above its 22 lowest bits.
_EPOCH_MS, _ID_SHIFT = 1_420_070_400_000, 22
SEEN, TAKEN, PARKED, NOISE = "\N{EYES}", "\N{WHITE HEAVY CHECK MARK}", "\N{SLEEPING SYMBOL}", "\N{NO ENTRY SIGN}"
_LINK = re.compile(r"(?:https?://|www\.|discord\.gg/)\S*", re.IGNORECASE)
_MENTION = re.compile(r"<(?:@[!&]?|#|a?:\w+:)\d+>|@(?:everyone|here)\b", re.IGNORECASE)


def path(places: Places) -> Path:
    return places.state / "chat.json"


def read(places: Places) -> dict[str, Any]:
    raw = store.read_json_or(path(places), None)
    if raw is None:
        return copy.deepcopy(_FRESH)
    if not isinstance(raw, dict) or raw.keys() != _FRESH.keys():
        raise store.StoreError(f"{path(places)}: want exactly {sorted(_FRESH)}")
    return raw


def note(ctx: Context) -> str | None:
    return imports.note(read(ctx.places))


def tag(settings: Settings) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w-]){re.escape(settings.get_str('chat.tag'))}(?![\w-])", re.IGNORECASE)


def _save(places: Places, state: dict[str, Any]) -> None:
    """The factory's own posts every cursor has passed are never read again, so they are forgotten."""
    oldest = min((int(cursor) for cursor in state["cursors"].values()), default=None)
    if oldest is not None:
        state["posted"] = [posted for posted in state["posted"] if int(posted) > oldest]
    store.write_json(path(places), state, mode=0o600)


def poll(ctx: Context) -> None:
    channels = ctx.settings().get_list("chat.channels")
    if not channels:
        return
    state = read(ctx.places)

    def work() -> str:
        now = ctx.clock.now()
        if state["wait_until"] is not None and now < state["wait_until"]:
            return f"Discord asked it to wait until {formats.iso(state['wait_until'])}"
        secret = discord.token(ctx.places)
        try:
            filed = sum(_read(ctx, state, secret, channel) for channel in channels)
            answered = _answer(ctx, state, secret, channels)
        except discord.Wait as exc:
            state["wait_until"] = now + exc.seconds
            raise
        state["wait_until"] = None
        return f"{filed} filed, {answered} answered"

    if imports.run(ctx, TIMED, state, work):
        _save(ctx.places, state)


def _read(ctx: Context, state: dict[str, Any], secret: str, channel: str) -> int:
    """File each tagged message the channel holds after its cursor, oldest first; how many were filed."""
    settings = ctx.settings()
    cursor = state["cursors"].get(channel)
    if cursor is None:
        state["cursors"][channel] = str((int(ctx.clock.now() * 1000) - _EPOCH_MS) << _ID_SHIFT)
        _save(ctx.places, state)
        return 0
    size, timeout = settings.get_int("chat.page_messages"), float(settings.get_int("chat.timeout_seconds"))
    tagged, senders, filed = tag(settings), set(settings.get_list("chat.senders")), 0
    for _ in range(settings.get_int("chat.poll_pages")):
        page = discord.messages(secret, channel, "after", cursor, size, timeout)
        for message in page:
            wanted = not senders or message.author in senders
            if wanted and message.id not in state["posted"] and tagged.search(message.text):
                filed += _file(ctx, state, channel, message)
            cursor = message.id
        state["cursors"][channel] = cursor
        _save(ctx.places, state)
        if len(page) < size:
            break
    return filed


def _file(ctx: Context, state: dict[str, Any], channel: str, message: Message) -> int:
    """1 when ``message`` is filed now, 0 when it was before."""
    rel = f"{CHAT}/{message.id}.md"
    queue = Queue.of(ctx)
    with ctx.lock, queue.lock():
        if message.id in state["filed"] or queue.files.exists(rel):
            return 0
        queue.files.write(rel, _words(channel, message).encode())
        body = f"A message tagged for the factory in the chat; its words are in {rel}."
        if message.replies_to is not None:
            body += " It replies to an earlier message, quoted there too, which is what it is about."
        sender, where = f"{formats.CHAT_FROM}{message.author}", f"chat channel {channel}, message {message.id}"
        req = intake.file_request(ctx, queue, CHAT, sender, "normal", where, body, message.id)
    state["filed"][message.id] = {"request": req.id, "channel": channel, "received": False, "answered": ""}
    _save(ctx.places, state)
    return 1


def _words(channel: str, message: Message) -> str:
    at = formats.one_line(message.at) if message.at else "unknown"
    head = f"---\nmessage: {message.id}\nchannel: {channel}\nfrom: {message.author}\nat: {at}\n---\n{NOTICE}"
    parts = [head, quoted(message.text)]
    replied = message.replies_to
    if replied is not None:
        parts += [f"It replies to message {replied.id}, from {replied.author}:", quoted(replied.text)]
    return "\n\n".join(parts) + "\n"


def _answer(ctx: Context, state: dict[str, Any], secret: str, channels: list[str]) -> int:
    """React and reply on each filed message whose request has news; how many answers went."""
    settings = ctx.settings()
    timeout, queue = float(settings.get_int("chat.timeout_seconds")), Queue.of(ctx)
    requests = {req.id: req for req in queue.requests()[0]} if state["filed"] else {}
    answered = 0
    for message, record in state["filed"].items():
        req, channel = requests.get(record["request"]), record["channel"]
        steps: list[tuple[str, object, str, str]] = []
        if not record["received"]:
            steps.append(("received", True, SEEN, f"Received — filed as {record['request']}."))
        if req is not None and req.outcome and req.outcome != record["answered"]:
            steps.append(("answered", req.outcome, *_triage(queue, req.outcome, settings)))
        for field, value, emoji, text in steps if channel in channels else []:
            nonce = hashlib.sha256(f"{message}:{field}:{value}".encode()).hexdigest()[:25]
            try:
                discord.react(secret, channel, message, emoji, timeout)
                state["posted"].append(discord.reply(secret, channel, message, text, nonce, timeout))
            except discord.Gone as exc:
                log.warning("warden: the chat's message %s in %s takes no answer (%s)", message, channel, exc)
            record[field] = value
            _save(ctx.places, state)
            answered += 1
    return answered


def _triage(queue: Queue, outcome: str, settings: Settings) -> tuple[str, str]:
    """The reaction and the reply that say what triage made of a request."""
    found = formats.outcome(outcome)
    if not found.task:
        return PARKED if found.kind == "parked" else NOISE, _line(f"Triage: {found.kind}", found.reason, settings)
    try:
        title = queue.task(found.task).title
    # telemetry: expected — the queue-format sweep reports a task that cannot be read; the reply names it, untitled
    except (FileNotFoundError, formats.FormatError):
        title = ""
    lead = "new task" if found.kind == "task" else "joined"
    return TAKEN, _line(f"Triage: {lead} {found.task}", title, settings)


def _line(lead: str, free: str, settings: Settings) -> str:
    """``lead``, then triage's ``free`` text on one line, with no link, mention or tag, cut to chat.reply_chars."""
    plain = formats.one_line(tag(settings).sub("", _MENTION.sub("", _LINK.sub("", free)))).rstrip(" .")
    most = settings.get_int("chat.reply_chars")
    if len(plain) > most:
        return f"{lead} — {plain[: most - 1].rstrip()}…"
    return f"{lead} — {plain}." if plain else f"{lead}."
