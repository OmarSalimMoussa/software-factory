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
        if isinstance(exc, OSError):
            raise  # a connection that dropped before the answer (RemoteDisconnected) is the OSError it is
        raise BrokenAnswer(asked.host, exc) from exc


class BrokenAnswer(OSError):
    """A host gave no whole HTTP answer: one cut short, or one that is not HTTP; ``kind`` names http.client's error."""

    def __init__(self, host: str, cause: http.client.HTTPException) -> None:
        self.kind = type(cause).__name__
        super().__init__(f"{host} gave no whole HTTP answer ({self.kind})")


def failure_class(exc: OSError) -> str:
    """The name of what failed under an error the door raised: the address's own error, http.client's, or ``exc``'s."""
    if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, BaseException):
        return type(exc.reason).__name__
    return exc.kind if isinstance(exc, BrokenAnswer) else type(exc).__name__
