"""The deploy log: each build staging (slot 0) and prod answered with, in the order the warden first saw it answer; and
the deploys in flight, while their restart may take the environment down for a moment.

The log is what the warden saw of the builds, and is never pruned. Push back reads prod's previous build here, the push
to prod every build staging answered with, and the dashboard each environment's current one. A deploy is in flight
from the warden's push until the environment answers on the new build, within the minutes the push gives it: a failed
probe then is the restart, never an outage.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from runtime.shared import store
from runtime.shared.paths import Places

_FIELDS = ("at", "env", "commit")


def record(places: Places, env: str, commit: str, at: str) -> None:
    """Log ``commit`` as ``env``'s build, unless it already is the last one logged for ``env``."""
    current = builds(places, env)["current"]
    if current is None or current["commit"] != commit:
        store.append_jsonl(places.deploys, {"at": at, "env": env, "commit": commit})


def log(places: Places, env: str) -> list[dict[str, str]]:
    """Every build ``env`` answered with, oldest first, each {at, env, commit}."""
    return [row for row in (_row(places, raw) for raw in store.read_jsonl(places.deploys)) if row["env"] == env]


def same_build(one: str, other: str) -> bool:
    """A health answer may name a build by a prefix of its commit."""
    return one.startswith(other) or other.startswith(one)


def builds(places: Places, env: str) -> dict[str, dict[str, str] | None]:
    """``env``'s current build, the last one logged, and its previous one, the latest earlier build with another
    commit; each {at, env, commit}, or None when the log holds none."""
    mine = log(places, env)
    current = mine[-1] if mine else None
    previous = None if current is None else next((r for r in reversed(mine) if r["commit"] != current["commit"]), None)
    return {"current": current, "previous": previous}


def _row(places: Places, raw: dict[str, Any]) -> dict[str, str]:
    if set(raw) != set(_FIELDS) or not all(isinstance(raw[k], str) and raw[k] for k in _FIELDS):
        raise store.StoreError(f"{places.deploys}: a record is not {{{', '.join(_FIELDS)}}}: {raw!r}"[:300])
    return {k: str(raw[k]) for k in _FIELDS}


def _flight_path(places: Places) -> Path:
    return places.state / "deploys-in-flight.json"


def _flights(places: Places) -> dict[str, dict[str, Any]]:
    raw = store.read_json_or(_flight_path(places), {})
    if not isinstance(raw, dict) or not all(isinstance(v, dict) and {"commit", "until", "landed"} <= set(v)
                                            for v in raw.values()):  # fmt: skip
        raise store.StoreError(f"{_flight_path(places)}: not a deploy per environment")
    return raw


def expect(places: Places, env: str, commit: str, at: float, minutes: int) -> None:
    """A deploy of ``commit`` to ``env`` began at ``at`` and lands within ``minutes``."""
    flights = _flights(places)
    flights[env] = {"commit": commit, "until": at + minutes * 60, "landed": False}
    store.write_json(_flight_path(places), flights)


def answered(places: Places, env: str, build: str) -> None:
    """``env`` answered on ``build``: a deploy of it has landed, and a failure from now on counts."""
    flights = _flights(places)
    flight = flights.get(env)
    if flight is not None and not flight["landed"] and same_build(flight["commit"], build):
        flight["landed"] = True
        store.write_json(_flight_path(places), flights)


def in_flight(places: Places, env: str, now: float) -> bool:
    """Whether a deploy to ``env`` is still landing: not answered on yet, and within its time."""
    flight = _flights(places).get(env)
    return flight is not None and not flight["landed"] and now < float(flight["until"])
