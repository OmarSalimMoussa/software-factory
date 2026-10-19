"""A fake Discord on the factory's network door: its channels' messages, and every call the factory made. Like
Discord, it answers a page newest first, and a status set for a method in place of that method's answer."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, cast

from queue_kit import FakeClock

from runtime.shared.network import Answer

TOKEN = "ZmFrZS1ib3Q.Gx1y2z.fake-token-for-tests-only-0000000000a1b2"
CHANNEL, OTHER = "700000000000000001", "700000000000000002"
ALICE, BRUNO, BOT = "700000000000000011", "700000000000000012", "700000000000000099"
_EPOCH_MS = 1_420_070_400_000


@dataclass
class FakeDiscord:
    clock: FakeClock
    channels: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    asked: list[tuple[str, str, dict[str, str]]] = field(default_factory=list)
    replies: list[dict[str, Any]] = field(default_factory=list)
    reactions: list[tuple[str, str, str]] = field(default_factory=list)
    status: dict[str, int] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    down: bool = False
    said: int = 0

    def say(self, channel: str, author: str, text: str, replies_to: str = "", bot: bool = False) -> str:
        self.said += 1
        ident = str(((int(self.clock.now() * 1000) - _EPOCH_MS) << 22) + self.said)
        writer = {"id": author, "username": f"u{author[-2:]}", "bot": bot}
        raw = {"id": ident, "author": writer, "timestamp": "2026-10-09T12:00:00+00:00", "content": text}
        if replies_to:
            raw["referenced_message"] = self.message(channel, replies_to)
        self.channels.setdefault(channel, []).append(raw)
        return ident

    def message(self, channel: str, ident: str) -> dict[str, Any]:
        return next(m for m in self.channels[channel] if m["id"] == ident)

    def send(self, request: urllib.request.Request, timeout: float) -> Answer:
        url = urllib.parse.urlsplit(request.full_url)
        method = request.get_method()
        self.asked.append((method, url.path, dict(request.header_items())))
        if self.down:
            raise ConnectionRefusedError(111, "Connection refused")
        if method in self.status:
            return Answer(self.status[method], self.headers, b"{}")
        parts = url.path.removeprefix("/api/v10/channels/").split("/")
        if method == "PUT":
            self.reactions.append((parts[0], parts[2], urllib.parse.unquote(parts[4])))
            return Answer(204, {}, b"")
        if method == "POST":
            body = json.loads(cast(bytes, request.data))
            self.replies.append(body)
            return Answer(
                200, {}, json.dumps(self.message(parts[0], self.say(parts[0], BOT, body["content"]))).encode()
            )
        page = self._page(parts[0], dict(urllib.parse.parse_qsl(url.query)))
        return Answer(200, {}, json.dumps(page[::-1]).encode())

    def _page(self, channel: str, query: dict[str, str]) -> list[dict[str, Any]]:
        every, limit = sorted(self.channels.get(channel, []), key=lambda m: int(m["id"])), int(query["limit"])
        if "after" in query:
            return [m for m in every if int(m["id"]) > int(query["after"])][:limit]
        if "before" in query:
            return [m for m in every if int(m["id"]) < int(query["before"])][-limit:]
        at = next(n for n, m in enumerate(every) if m["id"] == query["around"])
        return every[max(0, at - limit // 2) : at - limit // 2 + limit]
