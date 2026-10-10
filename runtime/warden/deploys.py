"""The deploy log: each build staging and prod answered with, in the order the warden first saw it answer.

The product's own CI and prod workflow do the deploying; the log is what the warden saw of them. Push back reads
prod's previous build here, and the dashboard reads each environment's current one.
"""

from __future__ import annotations

from typing import Any

from runtime.shared import store
from runtime.shared.paths import Places

_FIELDS = ("at", "env", "commit")


def record(places: Places, env: str, commit: str, at: str) -> None:
    """Log ``commit`` as ``env``'s build, unless it already is the last one logged for ``env``."""
    current = builds(places, env)["current"]
    if current is None or current["commit"] != commit:
        store.append_jsonl(places.deploys, {"at": at, "env": env, "commit": commit})


def builds(places: Places, env: str) -> dict[str, dict[str, str] | None]:
    """``env``'s current build, the last one logged, and its previous one, the latest earlier build with another
    commit; each {at, env, commit}, or None when the log holds none."""
    mine = [row for row in (_row(places, raw) for raw in store.read_jsonl(places.deploys)) if row["env"] == env]
    current = mine[-1] if mine else None
    previous = None if current is None else next((r for r in reversed(mine) if r["commit"] != current["commit"]), None)
    return {"current": current, "previous": previous}


def _row(places: Places, raw: dict[str, Any]) -> dict[str, str]:
    if set(raw) != set(_FIELDS) or not all(isinstance(raw[k], str) and raw[k] for k in _FIELDS):
        raise store.StoreError(f"{places.deploys}: a record is not {{{', '.join(_FIELDS)}}}: {raw!r}"[:300])
    return {k: str(raw[k]) for k in _FIELDS}
