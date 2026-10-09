"""The owner's messages to a session: the thread read back from its transcript, the box that sends the next one, and
why Open chat is greyed out when it is. Every word is chosen here; the warden says what happened to each message."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from runtime.dashboard.views import agentwords as words
from runtime.dashboard.views.kit import PartError, Snap, broken, when
from runtime.dashboard.views.shell import factory_state
from runtime.shared.models import BENCH_PENDING, DEPRIORITIZED, RETIRED, REVIVING, WAITING

_NOTES = {
    WAITING: "It is waiting for you, so it reads your message at once.",
    DEPRIORITIZED: "On the bench: it comes back with your message when a place is free.",
    BENCH_PENDING: "It is moving to the bench: send your message in a minute.",
    REVIVING: "It is coming back from the bench: send your message in a minute.",
}
_SUBAGENT_CHAT = "A subagent runs inside its parent's session: open the parent's chat."
_BENCHED_CHAT = "On the bench: no chat until it comes back."


def _at(stamp: str) -> str:
    """A transcript's time (ISO, with or without fractions of a second) as the page shows times."""
    try:
        return when(datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp())
    # telemetry: surfaced — the line draws without its time
    except ValueError:
        return ""


def thread(snap: Snap, row: dict[str, Any]) -> list[dict[str, Any]]:
    """Who said what, in order: each of the owner's messages, then the agent's replies to it."""
    try:
        entries = snap.get("thread")
    # telemetry: surfaced — the thread draws as one failed row; the rest of the panel still draws
    except PartError as exc:
        return [broken("thread", str(exc))]
    name = words.name(snap, row)
    out: list[dict[str, Any]] = []
    for entry in entries:
        out.append({"who": "You", "at": _at(entry["at"]), "text": entry["text"]})
        out += [{"who": name, "at": _at(reply["at"]), "text": reply["text"]} for reply in entry["replies"]]
    return out


def box(snap: Snap, row: dict[str, Any]) -> dict[str, Any] | None:
    """The box that sends the owner's next message; none for a subagent or a retired session."""
    if row["parent"] or row["state"] == RETIRED:
        return None
    benched = row["state"] == DEPRIORITIZED
    if benched and factory_state(snap) == "paused":
        note = "The factory is paused: resume it, then send your message."
    else:
        note = _NOTES.get(row["state"], "It reads your message as soon as its current turn ends.")
    name = "the orchestrator" if row["role"] == "orchestrator" else words.name(snap, row)
    return {
        "session": row["id"],
        "label": "Bring back with this message" if benched else "Send",
        "placeholder": f"Message {name}…",
        "max_chars": snap.settings.get_int("dashboard.message_max_chars"),
        "note": note,
    }


def chat(row: dict[str, Any]) -> tuple[str | None, str | None]:
    """The session's chat link, or why Open chat is greyed out."""
    if row["parent"]:
        return None, _SUBAGENT_CHAT
    if row["state"] in (DEPRIORITIZED, BENCH_PENDING):
        return None, _BENCHED_CHAT
    if not row["chat_link"]:
        if row["state"] == RETIRED:
            return None, "This session ended before its chat link appeared."
        return None, "The chat link appears a few seconds after the session starts."
    return str(row["chat_link"]), None


def orchestrator(snap: Snap) -> dict[str, Any] | None:
    """Home's box addressed to the orchestrator; none while no orchestrator runs."""
    live = [r for r in snap.get("sessions").values() if r["role"] == "orchestrator" and r["state"] != RETIRED]
    if not live:
        return None
    row = max(live, key=lambda r: str(r["started"]))
    found = box(snap, row)
    return None if found is None else {**found, "name": words.name(snap, row)}
