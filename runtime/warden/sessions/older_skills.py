"""Sessions whose context holds an older factory's skills: each one alive when the warden first starts on a new
install is told, once, as it next comes back, to load its skills again, since their steps may have changed.

A list of its own rather than a field on the registry's rows, so an earlier factory can still read the registry.
"""

from __future__ import annotations

from pathlib import Path

from runtime.shared import store
from runtime.shared.models import RETIRED
from runtime.shared.paths import Places
from runtime.warden.sessions import registry

RELOAD = (
    "The factory was upgraded while you were away, so the skills in your context may be out of date: load your "
    "role's skills again with the Skill tool before you go on."
)


def _path(places: Places) -> Path:
    return places.state / "older-skills.json"


def note_install(places: Places) -> int:
    """At the warden's start on an install it has not noted yet, every live session's skills are older; how many.

    The install is the tree the warden runs, which names its commit before the warden starts on it: the install's
    record is written only after, at its end. A factory run from a checkout has no such file and notes nothing.
    """
    path = places.install / "COMMIT"
    commit = path.read_text(encoding="utf-8").strip() if path.exists() else ""
    noted = store.read_json_or(_path(places), None)
    if not commit or (noted is not None and noted["commit"] == commit):
        return 0
    live = sorted(s.id for s in registry.read(places).values() if s.state != RETIRED and not s.parent)
    store.write_json(_path(places), {"commit": commit, "sessions": live}, mode=0o600)
    return len(live)


def holds(places: Places, sid: str) -> bool:
    noted = store.read_json_or(_path(places), None)
    return noted is not None and sid in noted["sessions"]


def told(places: Places, sid: str) -> None:
    """The session was told: it is not told again. The caller holds the warden's lock."""
    noted = store.read_json_or(_path(places), None)
    if noted is not None and sid in noted["sessions"]:
        left = [s for s in noted["sessions"] if s != sid]
        store.write_json(_path(places), {**noted, "sessions": left}, mode=0o600)
