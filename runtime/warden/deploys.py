"""The deploy log: each build staging and prod answered with, in the order the warden first saw it answer; and the
deploys in flight, while their restart may take the environment down for a moment.

The product's own CI and prod workflow do the deploying; the log is what the warden saw of them. Push back reads
prod's previous build here, and the dashboard reads each environment's current one. A deploy is in flight from the
warden's push (its own of the default branch, which CI takes to staging, or the owner's to prod) until the
environment answers on the new build, at most watch.deploy_minutes: a failed probe then is the restart, never an
outage or a red staging.
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
    if flight is not None and not flight["landed"] and (flight["commit"].startswith(build) or
                                                         build.startswith(flight["commit"])):  # fmt: skip
        flight["landed"] = True
        store.write_json(_flight_path(places), flights)


def in_flight(places: Places, env: str, now: float) -> bool:
    """Whether a deploy to ``env`` is still landing: not answered on yet, and within its time."""
    flight = _flights(places).get(env)
    return flight is not None and not flight["landed"] and now < float(flight["until"])


def last_push(places: Places, env: str) -> str | None:
    """The commit the warden last pushed toward ``env``, landed or not; None before its first push."""
    flight = _flights(places).get(env)
    return str(flight["commit"]) if flight is not None else None


def _branch_path(places: Places) -> Path:
    return places.state / "deploys-on-default.json"


def note_branch(places: Places, env: str, commit: str, on_default: bool | None, at: float) -> None:
    """Whether ``env``'s build ``commit`` is on the default branch, asked at ``at``; None when GitHub did not say."""
    raw = store.read_json_or(_branch_path(places), {})
    store.write_json(_branch_path(places), {**raw, env: {"commit": commit, "on_default": on_default, "at": at}})


def branch_asked(places: Places, env: str, commit: str) -> tuple[bool | None, float] | None:
    """The answer last noted for ``env``'s build ``commit`` and when it was asked; None when it never was."""
    found = store.read_json_or(_branch_path(places), {}).get(env)
    if not isinstance(found, dict) or found.get("commit") != commit or not isinstance(found.get("at"), int | float):
        return None
    value = found.get("on_default")
    return (value if isinstance(value, bool) else None), float(found["at"])


def on_default(places: Places, env: str) -> bool | None:
    """Whether ``env`` runs a build on the default branch: False for one GitHub's branch does not hold, None when
    unknown."""
    found = store.read_json_or(_branch_path(places), {}).get(env)
    current = builds(places, env)["current"]
    if not isinstance(found, dict) or current is None or found.get("commit") != current["commit"]:
        return None
    value = found.get("on_default")
    return value if isinstance(value, bool) else None
