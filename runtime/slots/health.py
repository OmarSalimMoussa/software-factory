"""Which commit a slot's stack runs: its health address, from ``scripts/dev health``, answers ``build_commit``.

The address comes from product code, so the warden asks only a loopback address on the slot's own port.
"""

from __future__ import annotations

import json
from urllib.parse import urlsplit

from runtime.shared import network
from runtime.slots import leases
from runtime.warden.context import Context

LOOPBACK = frozenset({"127.0.0.1", "localhost"})


def http_get(url: str, timeout: int, limit: int) -> bytes:
    body = network.call(url, timeout, limit=limit + 1).checked("the slot's stack").body
    if len(body) > limit:
        raise ValueError(f"the answer is larger than {limit} bytes")
    return body


class SlotUnread(Exception):
    """The slot's stack gave no commit; the message says why, in words the agent can act on."""


def _address(line: str, port: int) -> bool:
    """Whether ``line`` is a loopback http address on the slot's own port."""
    try:
        url = urlsplit(line)
        return url.scheme == "http" and url.hostname in LOOPBACK and url.port == port
    # telemetry: surfaced — a line urlsplit cannot read (a bracketed host that is no address) is no address: `False`
    except ValueError:
        return False


def _running(ctx: Context, slot: int, session: str) -> str:
    """The build_commit the slot's health address answers; SlotUnread naming why there is none."""
    settings = ctx.settings()
    result = ctx.launcher.run_stack(slot, ["health"], session)
    lines = [line.strip() for line in result.tail.splitlines() if line.strip()]
    if result.exit != 0 or not lines:
        raise SlotUnread(f"scripts/dev health exited {result.exit} with no address: bring the slot up first")
    port = leases.port(settings, slot)
    if not _address(lines[-1], port):
        raise SlotUnread(f"scripts/dev health must end with the slot's health address, http://127.0.0.1:{port}/…")
    try:
        raw = http_get(
            lines[-1], settings.get_int("slots.api_timeout_seconds"), settings.get_int("slots.api_max_kib") * 1024
        )
        commit = json.loads(raw).get("build_commit")
    except (OSError, ValueError, AttributeError) as exc:
        raise SlotUnread(f"the slot's health address gave no commit ({type(exc).__name__}): is the stack up?") from exc
    if not isinstance(commit, str) or not commit:
        raise SlotUnread("the slot's health answer has no build_commit")
    return commit


def off_head(ctx: Context, slot: int, session: str, head: str) -> str | None:
    """Why the slot's stack is not known to run ``head``, in words the agent can act on; None when it runs it."""
    try:
        commit = _running(ctx, slot, session)
    # telemetry: surfaced — what the slot lacks goes back as the reason, `str(exc)`, which the caller refuses with
    except SlotUnread as exc:
        return str(exc)
    if commit == leases.stand_in(slot):
        return (f"the slot answers {commit}, the stand-in its env file holds: the stack must take its build commit "
                "from the image it built, not from the env file")  # fmt: skip
    if head.startswith(commit) or commit.startswith(head):
        return None
    return f"your slot runs {commit[:12]}, not {head[:9]}: rebuild it at your head"
