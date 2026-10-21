"""Each session's "doing" sentence, built from facts the warden holds, never from what an agent claims.

The facts (slice x of N, the QA or critic round, the verdict of a hand-back) are recorded by the verbs that learn
them; the slot comes from the registry. A session may add one short line of its own with `factory doing`, which is
kept plain: no control characters and nothing a page could read as markup.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from runtime.shared import store
from runtime.shared.models import Session
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused
from runtime.shared.settings import Settings
from runtime.slots import leases

MAX_TEXT = 60
FACTS = ("slice", "slices", "qa_round", "critic_round", "verdict", "scenarios")
_UNSAFE = re.compile(r"[\x00-\x1f\x7f<>&\"'`]")


def facts_path(places: Places, session_id: str) -> Path:
    return places.doing_of(session_id)


def facts(places: Places, session_id: str) -> dict[str, Any]:
    raw = store.read_json_or(facts_path(places, session_id), {})
    if not isinstance(raw, dict) or set(raw) - set(FACTS):
        raise store.StoreError(f"{facts_path(places, session_id)}: not a facts record")
    return raw


def record(places: Places, session_id: str, **changes: int | str) -> None:
    """Record facts about a session; a verb calls this when it learns one (qa-ready, a round, a hand-back)."""
    unknown = set(changes) - set(FACTS)
    if unknown:
        raise ValueError(f"not doing facts: {sorted(unknown)}")
    merged = {**facts(places, session_id), **changes}
    store.write_json(facts_path(places, session_id), merged, mode=0o600)


def note_scenario(places: Places, session_id: str, name: str) -> None:
    """A scenario a regression tester read, which a slot 0 error first seen in its walk names."""
    record(places, session_id, scenarios=" ".join(dict.fromkeys([*scenarios(places, session_id), name])))


def scenarios(places: Places, session_id: str) -> list[str]:
    read = facts(places, session_id).get("scenarios")
    return str(read).split() if read else []


def clean(text: str) -> str:
    """The session's own line, checked; raises Refused naming what to change."""
    line = " ".join(text.split())
    if len(line) > MAX_TEXT:
        raise Refused("doing", f"say it in {MAX_TEXT} characters or fewer ({len(line)} now)")
    if _UNSAFE.search(line):
        raise Refused("doing", "use plain words: no quotes, angle brackets, ampersands or control characters")
    return line


def sentence(settings: Settings, session: Session, known: dict[str, Any], slot: int | None) -> str:
    """``slot``: the one the session's lease holds, or None."""
    parts: list[str] = []
    if slot is not None:
        parts.append(f"Slot {slot}")
    if known.get("slice") and known.get("slices"):
        parts.append(f"slice {known['slice']} of {known['slices']}")
    if known.get("critic_round"):
        parts.append(f"critic round {known['critic_round']} of {_rounds(settings, session, 'critic')}")
    if known.get("qa_round"):
        parts.append(f"QA round {known['qa_round']} of {_rounds(settings, session, 'qa')}")
    if known.get("verdict"):
        parts.append(f"handed back: {known['verdict']}")
    if session.doing:
        parts.append(session.doing)
    text = " · ".join(parts)
    return text[:1].upper() + text[1:]


def _rounds(settings: Settings, session: Session, kind: str) -> int:
    return settings.get_int(f"rounds.{kind}") + session.extensions.get(kind, 0)


def describe(places: Places, settings: Settings, session: Session) -> str:
    """The sentence the dashboard shows for ``session``."""
    lease = leases.held_by(places, settings, session.name)
    return sentence(settings, session, facts(places, session.id), lease.slot if lease else None)
