"""The standard set: every check, in the order a run reports them.

Each check is one module ``checks/standard/<id with underscores>.py`` holding ``ID``, a one-line ``RULE``,
``VALUES_FILE`` (its file in check-values/, or None when it reads none), ``VALUES`` (that file's schema) and
``run(repo, values) -> list[str]``, which returns one line per problem and an empty list when the rule holds; a
line that starts with ``NOTE`` is advice shown with the result, never a failure. A product never adds a check; it
only supplies values.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from checks.runner.repo import Repo
from checks.runner.values import Schema

IDS: tuple[str, ...] = (
    "repo-shape",
    "deploy-shape",
    "tools",
    "comment-budgets",
    "error-telemetry",
    "mirror-markers",
    "mirrored-constants",
    "history-narrative",
    "tree-rules",
    "testing-imports",
    "doc-refs",
    "no-console",
    "size-ceilings",
    "complexity-ratchet",
    "count-claims",
    "qa-scenarios",
    "env-template-sync",
    "network-door",
    "config-readers",
    "js-import-rules",
    "vocabulary-homes",
    "computation-owners",
    "quiet-modules",
    "image-sources",
    "pinned-config",
    "generated-current",
    "seeds-current",
    "queue-format",
    "tests",
)
NOTE = "note: "
# Files in check-values/ that feed something other than a check: the QA surfaces table.
OTHER_VALUES = ("surfaces.toml",)


@dataclass(frozen=True, slots=True)
class Check:
    id: str
    rule: str
    values_file: str | None
    values: Schema
    run: Callable[[Repo, dict[str, Any]], list[str]]


class CatalogError(Exception):
    """A standard check module is missing or malformed: a bug in the factory, never in the product."""


def load(check_id: str) -> Check:
    name = "checks.standard." + check_id.replace("-", "_")
    try:
        module = importlib.import_module(name)
    except ImportError as exc:
        raise CatalogError(f"{name} cannot be imported: {exc}") from exc
    missing = [a for a in ("ID", "RULE", "VALUES_FILE", "VALUES", "run") if not hasattr(module, a)]
    if missing or check_id != module.ID:
        raise CatalogError(
            f"{name} must define ID = {check_id!r}, RULE, VALUES_FILE, VALUES and run; " f"missing {missing}"
        )
    return Check(module.ID, module.RULE, module.VALUES_FILE, module.VALUES, module.run)


def values_files() -> set[str]:
    """Every file name check-values/ may hold."""
    names = {"baseline.json", *OTHER_VALUES}
    for check_id in IDS:
        check = load(check_id)
        if check.values_file is not None:
            names.add(check.values_file)
    return names
