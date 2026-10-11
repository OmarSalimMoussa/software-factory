"""Who may use the dashboard: a request to one of its own addresses, from a device the owner admitted with the
6-digit code once, and, for a write, a same-origin JSON request the browser could only have sent from the page.

A device's token lives in an HttpOnly, SameSite=Strict owner cookie (Secure on the tailnet's HTTPS name) and is
checked with the warden on every request. While the warden is away, a token it confirmed before still opens the
page's last known state until that token's own expiry.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from http.cookies import CookieError, SimpleCookie
from typing import Any
from urllib.parse import urlsplit

from runtime.dashboard.client import Warden, WardenDown
from runtime.shared.settings import Settings

COOKIE = "owner"
PAGE_POLICY = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
    "frame-ancestors 'none'"
)
WRITE_HEADER = "X-Software"
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


# Every header name is looked up in lower case: the server lowers them as they arrive.


class Denied(Exception):
    """The request is refused before it reaches the warden; ``status`` is the HTTP answer, the message says why."""

    def __init__(self, status: int, reason: str) -> None:
        self.status = status
        super().__init__(reason)


def _hostname(value: str) -> str:
    return (urlsplit(f"//{value}").hostname or "").lower()


def check_host(headers: Mapping[str, str], hosts: list[str], write: bool) -> str:
    """The name the browser used: Host, and Origin when sent (always, for a write), must each be one of
    ``dashboard.hosts``. A foreign Host is how a DNS-rebinding page would reach the dashboard; a foreign Origin is
    another site's page."""
    allowed = {h.lower() for h in hosts}
    host = _hostname(headers.get("host", ""))
    if host not in allowed:
        raise Denied(403, "This address is not one the dashboard answers.")
    origin = headers.get("origin")
    if origin is None:
        if write:
            raise Denied(403, "A change must come from the dashboard's own page.")
        return host
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https") or (parts.hostname or "").lower() not in allowed:
        raise Denied(403, "A change must come from the dashboard's own page.")
    return (parts.hostname or "").lower()


def check_write(headers: Mapping[str, str], body: Any) -> str:
    """A write is JSON, carries the page's own header and an idempotency key; returns the key."""
    if headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise Denied(415, "A change is sent as JSON.")
    if headers.get(WRITE_HEADER.lower()) != "1":
        raise Denied(403, "A change must come from the dashboard's own page.")
    if not isinstance(body, dict):
        raise Denied(400, "A change is one JSON object.")
    key = body.get("idempotency_key")
    if not isinstance(key, str) or not key:
        raise Denied(400, "A change carries an idempotency_key.")
    return key


def token_of(headers: Mapping[str, str]) -> str | None:
    jar: SimpleCookie = SimpleCookie()
    try:
        jar.load(headers.get("cookie", ""))
    # telemetry: expected — a cookie the browser mangled is no device token: the page asks for the code again
    except CookieError:
        return None
    morsel = jar.get(COOKIE)
    return morsel.value if morsel is not None and morsel.value else None


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _epoch(stamp: str) -> float:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()


class Devices:
    """Device tokens the warden confirmed, by digest, with their expiry, for the time it is away."""

    def __init__(self, warden: Warden, now: Callable[[], float]) -> None:
        self.warden, self.now = warden, now
        self._lock = threading.Lock()
        self._known: dict[str, float] = {}

    def admitted(self, token: str | None) -> bool:
        """Whether the token opens the dashboard; raises WardenDown when the warden is away and it never confirmed
        this token."""
        if not token:
            return False
        digest = _digest(token)
        try:
            answer = self.warden.call("device.verify", {"token": token})
        except WardenDown:
            with self._lock:
                until = self._known.get(digest)
            if until is None:
                raise
            return until > self.now()
        ok = answer.get("ok") is True and answer.get("device") == "ok"
        with self._lock:
            if ok:
                self._known[digest] = _epoch(str(answer["expires"]))
            else:
                self._known.pop(digest, None)
        return ok


def owner_cookie(token: str, host: str, settings: Settings) -> str:
    """The Set-Cookie value that admits this device; Secure wherever the page is served over HTTPS (the tailnet)."""
    age = settings.get_int("dashboard.device_days") * 86400
    secure = "" if host in _LOOPBACK else "; Secure"
    return f"{COOKIE}={token}; Path=/; Max-Age={age}; HttpOnly; SameSite=Strict{secure}"
