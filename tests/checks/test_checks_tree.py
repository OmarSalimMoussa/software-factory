from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from checks.runner.repo import Repo
from checks.standard import tree_rules

MakeRepo = Callable[[dict[str, str]], Path]
VALUES = """product_python = ["app"]
service_frameworks = []
entrypoints = {}
readme_exempt = {}
env_allow = {}
"""
DEFAULTED = (
    "app/conf.py:2: defaults an environment read, a silent fallback: load the key once, required, in the config module"
)


@pytest.mark.parametrize(
    ("line", "problems"),
    [
        ('VALUE = os.environ.get("A", "b")', [DEFAULTED]),
        ('VALUE = os.getenv("A") or "b"', [DEFAULTED]),
        ('VALUE = os.environ["A"]', []),
        ('"""Never write environ.get(k, d) or getenv(k) or d."""', []),
        ("# getenv(k, d) is a silent fallback", []),
    ],
    ids=["a-default", "an-or-default", "a-required-read", "a-docstring", "a-comment"],
)
def test_product_python_reads_no_environment_default(make_repo: MakeRepo, line: str, problems: list[str]) -> None:
    repo = Repo.open(make_repo({
        "check-values/tree-rules.toml": VALUES,
        "app/README.md": "# App\n",
        "app/conf.py": f"import os\n{line}\n",
    }))  # fmt: skip
    assert tree_rules.run(repo, repo.values(tree_rules.VALUES_FILE, tree_rules.VALUES)) == problems
