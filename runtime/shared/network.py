"""The factory's one door to the network: every outbound HTTP request in runtime/ goes through ``call``, or through
``upgrade`` for one to this machine that asks to switch protocols.

The caller sets the deadline, and may cap the body read (``limit``). An error status is an answer the caller reads,
and so is a redirect unless the caller follows them (``follow``). A request for this machine never goes through a
proxy the environment names. What does not answer raises OSError, TimeoutError when the deadline passed; ``checked``
turns a failed status into one too, for a caller that takes only success.
"""

from __future__ import annotations

import http.client
import re
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from email.message import Message
from http import HTTPStatus
from typing import Any
from urllib.parse import urlsplit

LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
_FOLD = re.compile(r"\r?\n[ \t]*")


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
    """Headers by lowercase name; a repeated one keeps each unfolded value on a line: a cookie's date holds a comma."""
    headers: dict[str, list[str]] = {}
    for name, value in found.items() if found is not None else []:
        headers.setdefault(name.lower(), []).append(_FOLD.sub(" ", value))
    return {name: "\n".join(values) for name, values in headers.items()}


def call(
    request: urllib.request.Request | str, timeout: float, *, limit: int | None = None, follow: bool = False
) -> Answer:
    asked = request if isinstance(request, urllib.request.Request) else urllib.request.Request(request)
    handlers: list[urllib.request.BaseHandler] = [] if follow else [_Stay()]
    if urlsplit(asked.full_url).hostname in LOOPBACK:
        handlers.append(urllib.request.ProxyHandler({}))
    try:
        with _whole(asked.host), urllib.request.build_opener(*handlers).open(asked, timeout=timeout) as reply:
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


def upgrade(request: urllib.request.Request, timeout: float, *, limit: int | None = None) -> Answer:
    """``request`` sent with its own Connection header, such as a websocket's `Upgrade`, which ``call`` replaces with
    `close` so that it reads each answer to its end. A 101 answer is its head alone, since the new protocol follows
    it; the connection closes after any answer."""
    if urlsplit(request.full_url).hostname not in LOOPBACK:
        raise ValueError(f"{request.host} is not this machine: an upgrade goes only to the loopback")
    link = http.client.HTTPConnection(request.host, timeout=timeout)
    try:
        with _whole(request.host):
            link.request(request.get_method(), request.selector, request.data, dict(request.header_items()))
            with link.getresponse() as reply:
                switched = reply.status == HTTPStatus.SWITCHING_PROTOCOLS
                return Answer(reply.status, _headers(reply.headers), b"" if switched else reply.read(limit))
    except TimeoutError as exc:
        raise TimeoutError(f"{request.host} did not answer in {timeout:g} s") from exc
    finally:
        link.close()


@contextmanager
def _whole(host: str) -> Iterator[None]:
    """An answer cut short or not HTTP raises BrokenAnswer."""
    try:
        yield
    except http.client.HTTPException as exc:
        if isinstance(exc, OSError):
            raise  # a connection that dropped before the answer (RemoteDisconnected) is the OSError it is
        raise BrokenAnswer(host, exc) from exc


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
