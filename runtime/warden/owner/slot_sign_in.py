"""The owner opens slot 0 signed in: a fresh one-time link from slot 0's own `scripts/dev sign-in`, for the test
address ``slots.owner_address`` names.

The link is product output, so only an address on slot 0's own origin goes back: ``slots.zero_origin`` when the box
opens staging to the world there, else loopback on slot 0's port. The dashboard keeps the answer, link and all, for
a retry of the same press; the product decides how long a link lasts, and it signs in once.
"""

from __future__ import annotations

from typing import Any

from runtime.qa.wrapper_tools import LINK
from runtime.shared.protocol import Refused, ok
from runtime.slots import leases
from runtime.slots.verbs import EMAIL
from runtime.warden.context import Context

SESSION = "owner-sign-in"  # who asked, in the stack unit's log


def open_slot(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    settings = ctx.settings()
    address = settings.get_str("slots.owner_address")
    if not EMAIL.match(address):
        raise Refused("sign-in", "Set the test address you sign in as, under Settings, Slots.")
    result = ctx.launcher.run_stack(0, ["sign-in", address], SESSION)
    links = [m.group(0) for m in LINK.finditer(result.tail)]
    if result.exit != 0 or not links:
        raise Refused("sign-in", f"Slot 0 gave no sign-in link (its sign-in exited {result.exit}). Is it up?")
    origin = leases.origin(settings, 0)
    if not links[0].startswith(f"{origin}/"):
        raise Refused("sign-in", f"Slot 0's sign-in link is not on its own address, {origin}, so it is not opened.")
    return ok(open=links[0])
