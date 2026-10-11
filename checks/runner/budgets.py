"""The two budget modes several checks share.

A **ceiling** fails a unit over the standard limit unless a pin in check-values/baseline.json records a higher limit
and its grounds. A **ratchet** fails a unit a branch makes worse than it is on the default branch, measured both
ways by the same check; only a branch's change is compared, so checking everything runs no ratchet.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

GROUNDS = ("behavior", "behaviour", "readab")
NARRATION = ("no longer", "previously", "formerly", "retained unchanged", "restoration of", "used to be")
_NARRATION_RE = re.compile(
    "|".join(
        [rf"\b{re.escape(p)}\b" for p in NARRATION] + [r"\btask \d", r"\bslice \d", r"\bqa[- ]fix", r"\bround \d"]
    ),
    re.IGNORECASE,
)

_SEAM_QUESTION = """\
  1. Does a written rule name a seam in this file? Routes stay thin; a module
     boundary is a directory plus its README; components take parameters; the
     import contracts. If one names where this file divides, split it: a move,
     proven by the unchanged test suite."""
_REWRITE = """\
  2. If no rule names a seam, the file is long but coherent. Raise the limit and
     REWRITE the grounds: one present-tense sentence saying why the file as it
     now stands cannot be split. Do not append a clause to the old reason."""


def reason_problems(gate: str, unit: str, reason: str, previous: str | None) -> list[str]:
    """Why a pin's reason is not honest grounds: it names no grounds, appends to the old one, or narrates."""
    if not any(g in reason.lower() for g in GROUNDS):
        return [f"{gate}: {unit} pin reason must name its grounds: behavior risk or readability"]
    out: list[str] = []
    if previous and reason != previous and reason.startswith(previous):
        out.append(
            f"{gate}: {unit} pin reason appends to the old one: rewrite the grounds to what is true of the " "file now"
        )
    narration = _NARRATION_RE.search(reason)
    if narration is not None:
        out.append(
            f"{gate}: {unit} pin reason narrates ({narration.group(0)!r}): state in the present tense why "
            "this file, as it stands, cannot be split"
        )
    return out


def over_ceiling(gate: str, unit: str, value: int, ceiling: int, remedy: str) -> str:
    return (
        f"{gate}: {unit} measures {value}, over the ceiling of {ceiling}, with no pin.\n"
        f"  {remedy}. Before pinning it, work the question a pin answers:\n{_SEAM_QUESTION}\n"
        "  If no rule names a seam, add a pin in check-values/baseline.json whose reason names its grounds\n"
        "  (behavior risk or readability) in one present-tense sentence."
    )


def over_pin(gate: str, unit: str, value: int, limit: int) -> str:
    return (
        f"{gate}: {unit} is pinned at {limit} and this change makes it {value}.\n"
        "  A pin is a ruling that this file's length is load-bearing, not a quota to top up.\n"
        f"{_SEAM_QUESTION}\n{_REWRITE}"
    )


def check_ceiling(
    gate: str, ceiling: int, measured: dict[str, int], stanza: dict[str, Any], previous: dict[str, Any], remedy: str
) -> list[str]:
    """Problems for every unit over ``ceiling`` without a sound pin; ``previous`` is the stanza at HEAD."""
    pins: dict[str, dict[str, Any]] = stanza["pins"]
    old_pins = previous.get("pins", {})
    out: list[str] = []
    for unit, value in sorted(measured.items()):
        if value <= ceiling:
            continue
        pin = pins.get(unit)
        if pin is None:
            out.append(over_ceiling(gate, unit, value, ceiling, remedy))
            continue
        old = old_pins.get(unit, {})
        found = reason_problems(gate, unit, pin["reason"], old.get("reason") if isinstance(old, dict) else None)
        if found:
            out += found
        elif value > pin["limit"]:
            out.append(over_pin(gate, unit, value, pin["limit"]))
    return out


def check_ratchet(
    gate: str, measured: Mapping[str, int], before: Mapping[str, int], *, new_units: bool = False
) -> list[str]:
    """Problems for every unit measuring over what it measured where the branch left the default branch. The default
    branch is the baseline: no number is kept by hand, and an improvement tightens it as soon as it merges. A unit the
    default branch lacks starts at zero, or, with ``new_units``, wherever the branch puts it."""
    out: list[str] = []
    for unit, value in sorted(measured.items()):
        if unit not in before and new_units:
            continue
        if value > before.get(unit, 0):
            out.append(f"{gate}: {unit} measures {value}, over the {before.get(unit, 0)} it measures on the default "
                       "branch")  # fmt: skip
    return out
