"""Which commit a slot's stack runs: its health address, from ``scripts/dev health``, answers ``build_commit``.

The address comes from product code, so the warden asks only a loopback address on the slot's own port.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from runtime.slots import leases
from runtime.warden.context import Context

LOOPBACK = frozenset({"127.0.0.1", "localhost"})


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


def http_get(url: str, timeout: int, limit: int) -> bytes:
    opener = urllib.request.build_opener(_NoRedirect())
    with opener.open(urllib.request.Request(url), timeout=timeout) as answer:
        body: bytes = answer.read(limit + 1)
    if len(body) > limit:
        raise ValueError(f"the answer is larger than {limit} bytes")
    return body


def running_commit(ctx: Context, slot: int, session: str) -> str | None:
    """The commit the slot's stack answers with, or None when it does not answer one."""
    settings = ctx.settings()
    result = ctx.launcher.run_stack(slot, ["health"], session)
    lines = [line.strip() for line in result.tail.splitlines() if line.strip()]
    if result.exit != 0 or not lines:
        return None
    url = urlsplit(lines[-1])
    if url.scheme != "http" or url.hostname not in LOOPBACK or url.port != leases.port(settings, slot):
        return None
    try:
        raw = http_get(
            lines[-1], settings.get_int("slots.api_timeout_seconds"), settings.get_int("slots.api_max_kib") * 1024
        )
        commit = json.loads(raw).get("build_commit")
    # telemetry: expected — a stack that is down or still starting has no commit; the caller says so
    except (OSError, ValueError, AttributeError):
        return None
    return commit if isinstance(commit, str) and commit else None
