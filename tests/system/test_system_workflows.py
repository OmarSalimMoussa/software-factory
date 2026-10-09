from __future__ import annotations

import re
from pathlib import Path

import pytest

FLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
REUSABLE = ("check.yml", "build.yml", "deploy-staging.yml", "promote.yml", "heartbeat.yml")


def _triggers(text: str) -> list[str]:
    block = text.split("\non:\n", 1)[1].split("\n\n", 1)[0]
    return re.findall(r"^  ([a-z_]+):", block, re.MULTILINE)


@pytest.mark.parametrize("name", REUSABLE)
def test_the_reusable_workflows_run_only_when_a_product_calls_them(name: str) -> None:
    assert _triggers((FLOWS / name).read_text(encoding="utf-8")) == ["workflow_call"]


def test_promote_refuses_any_actor_but_the_owner_and_any_commit_staging_does_not_run() -> None:
    text = (FLOWS / "promote.yml").read_text(encoding="utf-8")

    assert "ACTOR: ${{ github.actor }}" in text
    assert "TRIGGERING_ACTOR: ${{ github.triggering_actor }}" in text
    assert "OWNER_LOGIN: ${{ inputs.owner-login }}" in text
    assert '[ "$ACTOR" != "$OWNER_LOGIN" ] || [ "$TRIGGERING_ACTOR" != "$OWNER_LOGIN" ]' in text
    assert '[ "$wanted" != "$on_staging" ]' in text
    assert re.search(r"^    environment: prod$", text, re.MULTILINE)
    # Both checks come before the deploy.
    assert text.index("Only the owner promotes") < text.index("is what staging runs") < text.index("scripts/ci/promote")


def test_staging_deploys_never_reach_prod() -> None:
    text = (FLOWS / "deploy-staging.yml").read_text(encoding="utf-8")

    assert "scripts/ci/deploy-staging" in text
    assert "scripts/ci/promote" not in text
    assert "environment: staging" in text and "environment: prod" not in text


def test_the_factorys_own_ci_runs_its_tests_lint_and_types() -> None:
    text = (FLOWS / "ci.yml").read_text(encoding="utf-8")

    for command in ("pytest", "ruff check", "ruff format --check", "mypy"):
        assert f"--extra dev {command}" in text
