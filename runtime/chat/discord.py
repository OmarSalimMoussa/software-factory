"""The chat's service, Discord, as the warden alone calls it: a page of a channel's messages, a reply and a reaction,
as the bot whose token the owner gave with `chat.token`.

The token is kept with the warden's secrets and goes to Discord alone: no answer, error or log line holds it, and a
redirect is an answer, never followed. When Discord refuses one message a reply or a reaction (it was deleted, or
takes none), the call raises Gone; when it limits the rate, Wait, with the seconds it asks for; any other failure
raises ChatError, naming the call and the cause.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runtime.shared import network, store
from runtime.shared.claude_accounts import SHOWN
from runtime.shared.models import CHAT_ID
from runtime.shared.network import Answer
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused, ok
from runtime.system import layout
from runtime.warden.context import Context

API = "https://discord.com/api/v10"
TOKEN_FILE = "discord.token"
AGENT = "DiscordBot (software-factory, 1)"  # Discord turns away urllib's own agent; a bot names itself this way
POSITIONS = ("before", "after", "around")
_TOKEN = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_SECONDS = re.compile(r"[0-9]+(?:\.[0-9]+)?")


class ChatError(Exception):
    """The chat could not be read or answered; the message says which call and why, never the token."""


class Gone(ChatError):
    """Discord refused this one message the reply or the reaction."""


class Wait(ChatError):
    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        super().__init__(f"Discord asked the factory to wait {seconds:g} s")


@dataclass(frozen=True, slots=True)
class Message:
    id: str
    author: str  # the writer's id
    name: str | None  # the writer's name, as the chat shows it
    bot: bool
    at: str | None
    text: str
    replies_to: Message | None  # the message it replies to, when Discord sent it along


def send(request: urllib.request.Request, timeout: float) -> Answer:
    return network.call(request, timeout)


def token_path(places: Places) -> Path:
    return layout.secrets(places.state) / TOKEN_FILE


def token(places: Places) -> str:
    try:
        text = token_path(places).read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise ChatError("the owner has not given the chat bot's token: run chat.token") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise ChatError(f"the chat bot's token cannot be read ({type(exc).__name__})") from exc
    if not _TOKEN.fullmatch(text):
        raise ChatError("the chat bot's token file holds no token: run chat.token again")
    return text


def save_token(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """`chat.token`: the bot's token, as the request's bytes, kept with the warden's secrets in place of any kept
    before. Only its last characters are answered."""
    asked = "send the bot's token as the request's bytes, as the bot's page on Discord's Developer Portal shows it"
    try:
        given = (data or b"").decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise Refused("token", f"the token is not text: {asked}") from exc
    if not _TOKEN.fullmatch(given):
        raise Refused("token", f"that is not a bot token, three parts joined by dots: {asked}")
    store.write_text(token_path(ctx.places), given + "\n", mode=0o600)
    return ok(last4=given[-SHOWN:], message="Kept the chat bot's token; the warden reads the chat with it from now on.")


def _call(secret: str, method: str, path: str, timeout: float, body: dict[str, Any] | None = None) -> Any:
    """Discord's answer to one call, read as JSON; None when it answers with no content."""
    headers = {"Authorization": f"Bot {secret}", "User-Agent": AGENT}
    data = json.dumps(body).encode() if body is not None else b"" if method == "PUT" else None
    if body is not None:
        headers["Content-Type"] = "application/json"
    what = f"{method} {path.split('?')[0]}"
    try:
        answer = send(urllib.request.Request(f"{API}{path}", data=data, headers=headers, method=method), timeout)
    except OSError as exc:
        raise ChatError(f"Discord did not answer {what} ({network.failure_class(exc)})") from exc
    if answer.status == 429:
        raise _wait(answer, what)
    if answer.status == 401:
        raise ChatError(f"Discord refused the chat bot's token on {what}: run chat.token with a new one")
    if answer.status in (400, 404) and method != "GET":
        raise Gone(f"Discord answered {what} with HTTP {answer.status}")
    if not 200 <= answer.status < 300:
        raise ChatError(f"Discord answered {what} with HTTP {answer.status}")
    try:
        return json.loads(answer.body) if answer.body else None
    except ValueError as exc:
        raise ChatError(f"Discord answered {what} with something that is not JSON") from exc


def _wait(answer: Answer, what: str) -> ChatError:
    seconds = answer.headers.get("retry-after", "")
    if _SECONDS.fullmatch(seconds):
        return Wait(float(seconds))
    return ChatError(f"Discord limited the rate of {what} without saying for how long")


def _id(value: object) -> str:
    if not isinstance(value, str) or not CHAT_ID.fullmatch(value):
        raise ChatError("Discord sent a message without an id or a writer")
    return value


def _message(raw: Any) -> Message:
    found: dict[str, Any] = raw if isinstance(raw, dict) else {}
    author: dict[str, Any] = found["author"] if isinstance(found.get("author"), dict) else {}
    name = author.get("global_name") or author.get("username")
    replied = found.get("referenced_message")
    return Message(
        _id(found.get("id")),
        _id(author.get("id")),
        name if isinstance(name, str) else None,
        author.get("bot") is True,
        found["timestamp"] if isinstance(found.get("timestamp"), str) else None,
        found["content"] if isinstance(found.get("content"), str) else "",
        _message(replied) if isinstance(replied, dict) else None,
    )


def messages(secret: str, channel: str, position: str, ref: str, limit: int, timeout: float) -> list[Message]:
    """At most ``limit`` messages of the channel ``position`` the message ``ref``, oldest first."""
    if position not in POSITIONS or not (CHAT_ID.fullmatch(channel) and CHAT_ID.fullmatch(ref)):
        raise ValueError("a page of the chat is read before, after or around a message, by the ids of both")
    query = urllib.parse.urlencode({position: ref, "limit": limit})
    page = _call(secret, "GET", f"/channels/{channel}/messages?{query}", timeout)
    if not isinstance(page, list):
        raise ChatError("Discord's page of messages is not a list")
    return sorted(map(_message, page), key=lambda message: int(message.id))


def reply(secret: str, channel: str, message: str, text: str, nonce: str, timeout: float) -> str:
    """Post ``text`` as a reply to ``message`` that mentions no one; the id of the reply. Asked again with the same
    ``nonce`` within minutes, Discord answers with the first reply instead of posting a second."""
    body = {
        "content": text,
        "nonce": nonce,
        "enforce_nonce": True,
        "message_reference": {"message_id": message, "channel_id": channel},
        "allowed_mentions": {"parse": [], "replied_user": False},
    }
    return _message(_call(secret, "POST", f"/channels/{channel}/messages", timeout, body)).id


def react(secret: str, channel: str, message: str, emoji: str, timeout: float) -> None:
    _call(secret, "PUT", f"/channels/{channel}/messages/{message}/reactions/{urllib.parse.quote(emoji)}/@me", timeout)
