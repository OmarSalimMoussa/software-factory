"""What the product's ruff configuration excludes is not code: no check parses or measures it."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

from checks.runner.repo import Repo
from checks.standard import complexity_ratchet, history_narrative, testing_imports

MakeRepo = Callable[[dict[str, str]], Path]
BASELINE = json.dumps({"complexity": {"units": {}}, "history-narrative": {"units": {}}})
# Does not parse, and narrates history: every check below would object to it, were it code.
BROKEN = "def handler(:\n    pass  # previously this worked\n"


@pytest.mark.parametrize("check", [history_narrative, complexity_ratchet, testing_imports])
def test_a_file_the_products_ruff_excludes_is_never_read_as_code(make_repo: MakeRepo, check: ModuleType) -> None:
    root = make_repo(
        {
            "pyproject.toml": '[tool.ruff]\nextend-exclude = ["corpus"]\n',
            "check-values/baseline.json": BASELINE,
            "corpus/broken.py": BROKEN,
            "app/notes.py": "def notes() -> list[str]:\n    return []\n",
        }
    )

    assert check.run(Repo.open(root), {}) == []


def test_complexity_names_a_file_that_does_not_parse_once_instead_of_crashing(make_repo: MakeRepo) -> None:
    root = make_repo({"check-values/baseline.json": BASELINE, "app/broken.py": BROKEN + "class (:\n"})

    problems = complexity_ratchet.run(Repo.open(root), {})

    assert len(problems) == 1
    assert problems[0].startswith("complexity-ratchet: app/broken.py does not parse")
