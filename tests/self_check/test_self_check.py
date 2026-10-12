"""The factory meets the standard checks it holds every product to.

Every check in the catalog is gated here (it must pass on the factory's own tree), or sits in NOT_APPLICABLE (it fits
only a product, for the reason beside it). A check the catalog gains is gated at once. One rule no check reads is
held here too: an inline ignore names its code and says why. These tests carry the `self_check` marker: a plain run
leaves them out, and CI runs them as their own step, which the release waits for.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import tokenize
import tomllib
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
GATED = [check_id for check_id in catalog.IDS if check_id not in NOT_APPLICABLE]
# These compare a change with the commit it started from, so over the whole tree (--all) they have nothing to compare.
RATCHETS = ("comment-budgets", "history-narrative", "complexity-ratchet", "count-claims")

pytestmark = pytest.mark.self_check


def factory_check(check_id: str) -> tuple[int, str]:
    """The runner's verdict on one check over the factory's whole tree, as CI runs it: with no warden settings."""
    out = io.StringIO()
    return cli.run(["--all", "--only", check_id], ROOT, None, out), out.getvalue()


def base_of_this_push() -> str:
    """What this push is measured against: the default branch's previous tip on a push to it, else where the branch
    left the default branch. GitHub's own event file says which; CI checks out the whole history for it."""
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    default = event["repository"]["default_branch"]
    return str(event["before"]) if os.environ["GITHUB_REF_NAME"] == default else f"origin/{default}"


def test_each_listed_check_is_in_the_catalog_once_with_its_reason() -> None:
    assert sorted(set(NOT_APPLICABLE) - set(catalog.IDS)) == []
    assert [check_id for check_id, why in NOT_APPLICABLE.items() if not why.strip()] == []
    assert sorted(set(RATCHETS) - set(GATED)) == []


def test_lint_format_and_types_stay_gated() -> None:
    # CI runs ruff and mypy only through the tools check, so listing it anywhere but the gated set would stop them;
    # and the tools check runs mypy only over the packages pyproject's [tool.mypy] names.
    assert "tools" in GATED
    assert tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["mypy"]["packages"]


@pytest.mark.parametrize("check_id", GATED)
def test_the_factory_passes_each_gated_check(check_id: str) -> None:
    status, output = factory_check(check_id)
    assert status == 0, output


def test_every_inline_ignore_names_its_code_and_says_why() -> None:
    # Ruff refuses a blanket or unused noqa, and mypy an unused ignore; neither asks why, so this does.
    listed = subprocess.run(["git", "-C", str(ROOT), "ls-files", "*.py"], capture_output=True, text=True, check=True)
    unexplained = []
    for rel in listed.stdout.split():
        with (ROOT / rel).open(encoding="utf-8") as source:
            for token in tokenize.generate_tokens(source.readline):
                said = token.string
                if token.type == tokenize.COMMENT and _SUPPRESSION.search(said) and not _REASONED.search(said):
                    unexplained.append(f"{rel}:{token.start[0]}: {said}")
    assert unexplained == []


_SUPPRESSION = re.compile(r"#\s*(noqa|type:\s*ignore)\b")
# The reason follows the codes on the line it excuses: after " - " for ruff's, after a second "#" for mypy's.
_REASONED = re.compile(r"# noqa: [A-Z]+\d+(, [A-Z]+\d+)* - \S|# type: ignore\[[a-z-]+(, [a-z-]+)*\]\s+# \S")


@pytest.mark.parametrize("check_id", RATCHETS)
def test_this_push_holds_each_ratchet(check_id: str) -> None:
    out = io.StringIO()
    status = cli.run(["--since", base_of_this_push(), "--only", check_id], ROOT, None, out)
    assert status == 0, out.getvalue()
