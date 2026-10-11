from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from checks.runner.repo import Repo
from checks.standard import doc_refs

MakeRepo = Callable[[dict[str, str]], Path]


@pytest.mark.parametrize(
    ("token", "problems"),
    [
        ("app/main.py", []),
        ("app/gone.py", ["docs/guide.md: `app/gone.py` does not resolve"]),
        ("~/.config/tool/settings.json", []),
    ],
    ids=["a-tracked-path", "a-missing-path", "a-path-in-a-home-folder"],
)
def test_a_living_doc_names_only_paths_in_the_repo_that_exist(
    make_repo: MakeRepo, token: str, problems: list[str]
) -> None:
    repo = Repo.open(make_repo({
        "check-values/doc-refs.toml": "excluded = {}\n",
        "app/main.py": "VALUE = 1\n",
        "docs/guide.md": f"# Guide\n\nRead `{token}` first.\n",
    }))  # fmt: skip
    assert doc_refs.run(repo, repo.values(doc_refs.VALUES_FILE, doc_refs.VALUES)) == problems
