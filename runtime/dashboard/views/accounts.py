"""The Settings page's Claude account section: each account the agents may sign in with, the one they use, and the
actions on each, as the warden lists them. A token is never part of it; only its last characters are."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from runtime.dashboard.views.kit import Snap, epoch, when

TITLE = "Claude account"
HINT = (
    "The agents sign in to Claude with the active account. Switching restarts their Claude daemon, which stops every "
    "agent for a moment; each one that was working comes back where it left off."
)


def _row(row: dict[str, Any]) -> dict[str, Any]:
    if row.get("broken"):
        return row
    name, url = row["name"], f"/api/accounts/{quote(row['name'])}"
    idle = not row["active"]
    return {
        "name": name,
        "active": row["active"],
        "chip": "active" if row["active"] else None,
        "token": "…" + row["last4"],
        "added": f"Added {when(epoch(row['added']), utc=True)}",
        "switch": {"label": "Switch", "url": f"{url}/use", "aria": f"Switch the agents to {name}"} if idle else None,
        "remove": {
            "label": "Remove",
            "url": f"{url}/remove",
            "title": f"Remove {name}?",
            "body": f"The factory forgets {name}'s token. Add it again any time with Add account.",
            "keep": "Keep it",
        }
        if idle
        else None,
    }


def section(snap: Snap) -> dict[str, Any]:
    rows = [_row(row) for row in snap.get("accounts")]
    return {
        "title": TITLE,
        "hint": HINT,
        "rows": rows,
        "empty": None if rows else "No account yet. Add the one `claude setup-token` gives you.",
        "add": {
            "title": "Add account",
            "url": "/api/accounts",
            "name": "Name",
            "name_hint": "Lower-case letters, digits and dashes, such as work.",
            "token": "Token",
            "token_hint": "What `claude setup-token` printed. It is never shown again.",
            "button": "Add account",
        },
    }
