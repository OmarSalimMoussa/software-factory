from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from checks.runner import cli, values
from checks.runner.repo import Repo
from checks.runner.values import PATTERN, ValuesError

TREE_RULES = """\
product_python = ["app"]
service_frameworks = []
[entrypoints]
[env_allow]
[readme_exempt]
"""


def test_every_problem_in_a_values_file_is_listed_at_once(tmp_path: Path) -> None:
    (tmp_path / "check-values").mkdir()
    (tmp_path / "check-values" / "x.toml").write_text('roots = "app"\nextra = 1\ncalls = ["(open"]\n[allow]\n"a" = 3\n')
    schema = {"roots": [str], "calls": [PATTERN], "allow": {str: str}, "remedy": str}
    with pytest.raises(ValuesError) as caught:
        values.load(tmp_path, "x.toml", schema)
    assert sorted(caught.value.problems) == [
        "check-values/x.toml is missing remedy",
        "check-values/x.toml.allow.a must be a string",
        "check-values/x.toml.calls[0] does not compile as a regular expression: missing ), unterminated subpattern "
        "at position 0",
        "check-values/x.toml.extra is not a known value",
        "check-values/x.toml.roots must be a list",
    ]


def test_a_check_without_its_values_file_fails_naming_it(make_repo: Callable[[dict[str, str]], Path]) -> None:
    root = make_repo({"README.md": "# A product\n", "app/README.md": "# App\n", "app/main.py": "X = 1\n"})
    assert cli.run_one("tree-rules", Repo.open(root)) == [
        "check-values/tree-rules.toml is missing: every product supplies it (an empty list means nothing is listed)"
    ]


@pytest.mark.parametrize(
    ("exempt", "said"),
    [
        ('"app/data" = "a corpus read only by its tests"', []),
        ('"web" = "the page"', ["app/data: no README.md", "stale exemption: remove web from readme_exempt"]),
    ],
)
def test_an_exemption_answers_a_live_case_or_fails_as_stale(
    make_repo: Callable[[dict[str, str]], Path], exempt: str, said: list[str]
) -> None:
    root = make_repo(
        {
            "README.md": "# A product\n",
            "app/README.md": "# App\n",
            "app/main.py": "X = 1\n",
            "app/data/one.json": "{}\n",
            "check-values/tree-rules.toml": TREE_RULES + exempt + "\n",
        }
    )
    problems = cli.run_one("tree-rules", Repo.open(root))
    assert len(problems) == len(said)
    assert all(any(s in p for p in problems) for s in said), problems
