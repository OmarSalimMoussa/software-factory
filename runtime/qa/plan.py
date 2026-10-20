"""Which testers a change needs: a fixed table, never judgment (factory spec, "Which testers a change needs").

The surfaces come from two sources joined: the task's own, and those the branch's diff touches by the path
prefixes in check-values/surfaces.toml. The required set is their union, so a diff that touches the UI cannot skip
the UI testers. A small backend fix (rung 1) runs no backend tester.

surfaces.toml maps a surface to the path prefixes that belong to it: ``ui = ["web/"]``, ``api = ["server/"]``.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterable, Mapping, Sequence

from runtime.shared.models import SURFACES

SCREENS = "ui"  # the surface whose browser testers walk it at every width (qa.widths)
TESTERS: dict[str, tuple[str, ...]] = {
    SCREENS: ("qa-ui-function", "qa-ux", "qa-visual"),
    "api": ("qa-backend",),
    "access": ("qa-security",),
    "desktop": ("qa-ui-function",),
    "none": (),
}
# The rung from which a backend change is large enough for its tester.
BACKEND_FROM_RUNG = 2


class SurfacesError(ValueError):
    """check-values/surfaces.toml cannot be read as surfaces and path prefixes."""


def surface_prefixes(text: str) -> dict[str, list[str]]:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SurfacesError(f"surfaces.toml is not TOML: {exc}") from exc
    problems = [f"{key} is not a surface" for key in raw if key not in SURFACES or key == "none"]
    problems += [
        f"{key} must list path prefixes"
        for key, value in raw.items()
        if not isinstance(value, list) or not all(isinstance(p, str) and p.strip() for p in value)
    ]
    if problems:
        raise SurfacesError("surfaces.toml: " + "; ".join(problems))
    return {key: list(value) for key, value in raw.items()}


def diff_surfaces(paths: Iterable[str], prefixes: Mapping[str, Sequence[str]]) -> dict[str, list[str]]:
    """Each surface the changed paths touch, with the paths that touch it."""
    touched: dict[str, list[str]] = {}
    for path in paths:
        for surface, starts in prefixes.items():
            if any(path.startswith(start) for start in starts):
                touched.setdefault(surface, []).append(path)
    return touched


def required(task_surfaces: Sequence[str], rung: int, diff: Mapping[str, Sequence[str]]) -> dict[str, list[str]]:
    """Every required tester with the reasons it is required, in a stable order."""
    reasons: dict[str, list[str]] = {}
    sources = [(s, "the task names it") for s in task_surfaces]
    sources += [
        (s, f"the diff touches {paths[0]}" + (f" and {len(paths) - 1} more" if len(paths) > 1 else ""))
        for s, paths in diff.items()
    ]
    for surface, why in sources:
        if surface == "api" and rung < BACKEND_FROM_RUNG:
            continue
        for tester in TESTERS[surface]:
            where = " on the remote desktop" if surface == "desktop" else ""
            reasons.setdefault(tester, []).append(f"{surface}{where}: {why}")
    order = [t for s in SURFACES for t in TESTERS[s]]
    return {tester: reasons[tester] for tester in sorted(reasons, key=order.index)}
