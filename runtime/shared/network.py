"""The factory's one door to the network: every outbound HTTP request in runtime/ goes through ``call``.

The caller sets the deadline, and may cap the body read (``limit``). An error status is an answer the caller reads,
and so is a redirect unless the caller follows them (``follow``). A request for this machine never goes through a
proxy the environment names. What does not answer raises OSError, TimeoutError when the deadline passed; ``checked``
turns a failed status into one too, for a caller that takes only success.
"""

from __future__ import annotations

import http.client
import urllib.error
import urllib.request
from dataclasses import dataclass
from email.message import Message
from typing import Any
from urllib.parse import urlsplit

LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


class StatusError(OSError):
    """An answer whose status says the request failed, raised by ``checked``."""


@dataclass(frozen=True, slots=True)
class Answer:
    status: int
    headers: dict[str, str]
    body: bytes

    def checked(self, what: str) -> Answer:
        if not 200 <= self.status < 300:
            raise StatusError(f"{what} answered HTTP {self.status}")
        return self


class _Stay(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _headers(found: Message | None) -> dict[str, str]:
    return {k.lower(): v for k, v in found.items()} if found is not None else {}


def call(
    request: urllib.request.Request | str, timeout: float, *, limit: int | None = None, follow: bool = False
) -> Answer:
    asked = request if isinstance(request, urllib.request.Request) else urllib.request.Request(request)
    handlers: list[urllib.request.BaseHandler] = [] if follow else [_Stay()]
    if urlsplit(asked.full_url).hostname in LOOPBACK:
        handlers.append(urllib.request.ProxyHandler({}))
    try:
        with urllib.request.build_opener(*handlers).open(asked, timeout=timeout) as reply:
            return Answer(reply.status, _headers(reply.headers), reply.read(limit))
    # telemetry: expected — an error status is an answer: the caller reads its status and body
    except urllib.error.HTTPError as exc:
        with exc:
            body = b"" if exc.fp is None else exc.read() if limit is None else exc.read(limit)
            return Answer(exc.code, _headers(exc.headers), body)
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise TimeoutError(f"{asked.host} did not answer in {timeout:g} s") from exc
        raise
    except http.client.HTTPException as exc:
        raise OSError(f"{asked.host} answered with something that is not HTTP ({type(exc).__name__})") from exc
