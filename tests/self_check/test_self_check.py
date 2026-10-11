"""The factory meets the standard checks it holds every product to.

Every check in the catalog is gated here (it must pass on the factory's own tree), waits in NOT_YET (it applies but
still fails, until the slice named beside it lands), or sits in NOT_APPLICABLE (it fits only a product, for the reason
beside it). A check the catalog gains is gated at once. These tests carry the `self_check` marker: a plain run leaves
them out, and CI runs them as their own step, which the release waits for.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from checks.runner import catalog, cli

ROOT = Path(__file__).resolve().parents[2]

NOT_APPLICABLE = {
    "repo-shape": "wants GOAL.md, queue/, qa/scenarios/ and scripts/dev: a product's shape, not the tool's",
    "deploy-shape": "wants the product CI templates, the prod deploy and factory.toml; the factory ships a release",
    "qa-scenarios": "describes what a product's users do; the factory has no users but its owner",
    "seeds-current": "checks a product's seed data through scripts/dev; the factory has no seed data",
    "tests": "runs a product's tests through its test map; the factory's CI runs its own pytest directly",
    "image-sources": "checks the images a product builds; the factory builds none",
}
NOT_YET = {
    "doc-refs": "slice 2",
    "mirror-markers": "slice 2",
    "mirrored-constants": "slice 2",
    "tree-rules": "slice 3",
    "config-readers": "slice 4",
    "size-ceilings": "slice 5",
    "error-telemetry": "slice 6",
    "network-door": "slice 7",
    "computation-owners": "slice 7",
    "js-import-rules": "slice 7",
    "vocabulary-homes": "slice 7",
    "quiet-modules": "slice 7",
    "pinned-config": "slice 7",
    "generated-current": "slice 7",
}
GATED = [check_id for check_id in catalog.IDS if check_id not in NOT_APPLICABLE and check_id not in NOT_YET]

pytestmark = pytest.mark.self_check


def factory_check(check_id: str) -> tuple[int, str]:
    """The runner's verdict on one check over the factory's whole tree, as CI runs it: with no warden settings."""
    out = io.StringIO()
    return cli.run(["--all", "--only", check_id], ROOT, None, out), out.getvalue()


def test_each_listed_check_is_in_the_catalog_once_with_its_reason() -> None:
    listed = [*NOT_APPLICABLE, *NOT_YET]
    assert sorted(set(listed) - set(catalog.IDS)) == []
    assert sorted(set(NOT_APPLICABLE) & set(NOT_YET)) == []
    assert [check_id for check_id, why in {**NOT_APPLICABLE, **NOT_YET}.items() if not why.strip()] == []


def test_lint_format_and_types_stay_gated() -> None:
    # CI runs ruff and mypy only through the tools check, so listing it anywhere but the gated set would stop them.
    assert "tools" in GATED


@pytest.mark.parametrize("check_id", GATED)
def test_the_factory_passes_each_gated_check(check_id: str) -> None:
    status, output = factory_check(check_id)
    assert status == 0, output


@pytest.mark.parametrize("check_id", sorted(NOT_YET))
def test_a_check_not_yet_gated_still_fails(check_id: str) -> None:
    status, output = factory_check(check_id)
    if status == 0:
        pytest.fail(f"{check_id} passes now: move it from NOT_YET to the gated set\n{output}")
    assert status == 1, f"{check_id} could not run (exit {status}), which is no verdict\n{output}"
