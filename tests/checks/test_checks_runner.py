from __future__ import annotations

import io
import subprocess
import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import MakeRepo, commit_all

from checks.runner import cli
from checks.runner.repo import Repo
from checks.standard import count_claims
from checks.standard import tests as tests_check
from runtime.shared.settings import Settings, build

FACTORY = Path(__file__).resolve().parents[2]


def settings() -> Settings:
    defaults = tomllib.loads((FACTORY / "settings.toml").read_text())
    return build(
        defaults,
        {
            "repo.path": "/product",
            "repo.default_branch": "main",
            "repo.github": "owner/product",
        },
    )


def check(root: Path, *argv: str) -> tuple[int, str]:
    out = io.StringIO()
    return cli.run(list(argv), root, settings(), out), out.getvalue()


def test_the_whole_set_passes_on_the_fixture(fixture_product: Path) -> None:
    out = io.StringIO()
    assert cli.run(["--all"], fixture_product, None, out) == 0, out.getvalue()


def test_since_a_commit_checks_only_what_head_adds_to_it(
    fixture_product: Path, commit: Callable[[Path, str], None]
) -> None:
    subprocess.run(["git", "checkout", "-q", "-b", "work"], cwd=fixture_product, check=True)
    notes = fixture_product / "app" / "notes.py"
    notes.write_text(notes.read_text() + "\n\nLIMIT = 500\n")
    commit(fixture_product, "a limit")
    first = subprocess.run(["git", "rev-parse", "HEAD"], cwd=fixture_product, capture_output=True, text=True,
                           check=True).stdout.strip()  # fmt: skip
    (fixture_product / "qa" / "scenarios" / "README.md").write_text("# QA scenarios\n\nEach opens with its labels.\n")
    commit(fixture_product, "words")

    assert "over 2 changed path(s)" in check(fixture_product, "--only", "qa-scenarios")[1]
    assert "over 1 changed path(s)" in check(fixture_product, "--only", "qa-scenarios", "--since", first)[1]


@pytest.mark.parametrize(
    ("since", "status", "said"),
    [(True, 0, "over 1 changed path(s)"), (False, 2, "with no settings, run factory check --all, or --since")],
    ids=["since-a-commit", "a-bare-branch-run"],
)
def test_without_settings_only_a_named_base_or_everything_runs(
    fixture_product: Path, commit: Callable[[Path, str], None], since: bool, status: int, said: str
) -> None:
    subprocess.run(["git", "checkout", "-q", "-b", "work"], cwd=fixture_product, check=True)
    first = subprocess.run(["git", "rev-parse", "HEAD"], cwd=fixture_product, capture_output=True, text=True,
                           check=True).stdout.strip()  # fmt: skip
    (fixture_product / "qa" / "scenarios" / "README.md").write_text("# QA scenarios\n\nEach opens with its labels.\n")
    commit(fixture_product, "words")
    out = io.StringIO()
    argv = ["--only", "qa-scenarios", *(["--since", first] if since else [])]
    assert (cli.run(argv, fixture_product, None, out), said in out.getvalue()) == (status, True), out.getvalue()


def test_an_uncommitted_change_is_refused(fixture_product: Path, commit: Callable[[Path, str], None]) -> None:
    subprocess.run(["git", "checkout", "-q", "-b", "work"], cwd=fixture_product, check=True)
    (fixture_product / "qa" / "scenarios" / "README.md").write_text("# QA scenarios\n\nEach opens with its labels.\n")
    status, output = check(fixture_product)
    assert status == 2 and "commit your changes first" in output and "(M qa/scenarios/README.md)" in output


@pytest.mark.parametrize(
    ("script", "said"),
    [("sleep 5", "passed 1 s, so it was stopped"), ('echo "per test: $FACTORY_TEST_SECONDS"; exit 1', "per test: 45")],
    ids=["hung-run", "per-test-limit-given"],
)
def test_a_test_run_on_the_laptop_has_its_time_limits(make_repo: Callable[..., Path], script: str, said: str) -> None:
    root = make_repo({"scripts/dev*": f"#!/bin/sh\n{script}\n"})
    repo = Repo.open(root)
    repo.test_seconds = (45, 1)
    values = {"always": [], "full": [], "skip_suffixes": [], "readers": {}}

    problems = tests_check.run(repo, values)
    assert len(problems) == 1 and said in problems[0]


@pytest.mark.parametrize("limits", [None, (45, 60)], ids=["no-limits", "laptop-limits"])
def test_a_failed_test_runs_report_holds_what_it_printed_in_order_so_its_summary_ends_it(
    make_repo: Callable[..., Path], limits: tuple[int, int] | None
) -> None:
    script = "echo noise >&2; echo 'FAIL relay/shutdown.test.ts'; echo more noise >&2; echo '1 failed'; exit 1"
    repo = Repo.open(make_repo({"scripts/dev*": f"#!/bin/sh\n{script}\n"}))
    repo.test_seconds = limits

    [problem] = tests_check.run(repo, {"always": [], "full": [], "skip_suffixes": [], "readers": {}})

    assert problem.endswith("noise\nFAIL relay/shutdown.test.ts\nmore noise\n1 failed")


@pytest.mark.parametrize(
    ("changed", "picked", "unplaced"),
    [
        (("app/notes.py", "README.md"), ["tests"], []),
        (("app/sub/deep.py", "web/app.js"), ["sub-tests", "tests"], []),
        (("README.md", "qa/scenarios/x.md"), [], []),
        (("pyproject.toml",), None, []),
        (("Makefile",), None, ["Makefile"]),
    ],
)
def test_changed_paths_pick_tests_by_their_longest_prefix(
    changed: tuple[str, ...], picked: list[str] | None, unplaced: list[str]
) -> None:
    values = {
        "always": [],
        "full": ["pyproject.toml"],
        "skip_suffixes": [".md"],
        "readers": {"app/": ["tests"], "app/sub/": ["sub-tests", "tests"], "web/": []},
    }
    assert tests_check.select(changed, values) == (picked, unplaced)


def _branch_from(root: Path, files: dict[str, str]) -> Repo:
    """The default branch as committed, then a branch that writes ``files``: the repo as a branch check opens it."""
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True).stdout
    subprocess.run(["git", "checkout", "-q", "-b", "work"], cwd=root, check=True)
    for rel, text in files.items():
        (root / rel).write_text(text)
    commit_all(root, "work")
    return Repo.open(root, tuple(files), base.strip())


@pytest.mark.parametrize(("now", "fails"), [("all three of them, the last two", True), ("every one", False)])
def test_a_ratchet_compares_what_a_branch_changed_with_the_default_branch(
    make_repo: MakeRepo, now: str, fails: bool
) -> None:
    root = make_repo({"docs/a.md": "We keep all three of them.\n", "docs/b.md": "All five agree.\n"})

    repo = _branch_from(root, {"docs/a.md": f"We keep {now}.\n"})

    problems = count_claims.run(repo, {})
    assert bool(problems) is fails  # docs/b.md, unchanged, is never measured
    assert not fails or problems[0].startswith("count-claims: docs/a.md has 2, over the 1 it has on the default branch")


@pytest.mark.parametrize(("now", "fails"), [("all three of them", False), ("all three of them, the last two", True)])
def test_a_ratchet_compares_a_file_the_branch_moved_with_the_file_it_came_from(
    make_repo: MakeRepo, now: str, fails: bool
) -> None:
    page = "We keep {}.\n" + "".join(f"Line {n} of the page says what it says.\n" for n in range(20))
    root = make_repo({"docs/a.md": page.format("all three of them")})
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True).stdout
    subprocess.run(["git", "checkout", "-q", "-b", "work"], cwd=root, check=True)
    (root / "docs" / "moved").mkdir()
    subprocess.run(["git", "mv", "docs/a.md", "docs/moved/a.md"], cwd=root, check=True)
    (root / "docs" / "moved" / "a.md").write_text(page.format(now))
    commit_all(root, "work")

    problems = count_claims.run(Repo.open(root, ("docs/a.md", "docs/moved/a.md"), base.strip()), {})

    assert bool(problems) is fails
    assert not fails or problems[0].startswith("count-claims: docs/moved/a.md has 2, over the 1 it has on the default")


@pytest.mark.parametrize(("extracted", "fails"), [(True, False), (False, True)], ids=["taken-out", "copied-whole"])
def test_a_ratchet_compares_a_copy_with_its_source_only_when_the_branch_took_it_out_of_it(
    make_repo: MakeRepo, extracted: bool, fails: bool
) -> None:
    claims = "We keep all three of them.\n" + "".join(f"Line {n} of the page says what it says.\n" for n in range(20))
    rest = "".join(f"Another line, {n}, stays.\n" for n in range(5))
    root = make_repo({"docs/a.md": claims + rest})
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True).stdout
    subprocess.run(["git", "checkout", "-q", "-b", "work"], cwd=root, check=True)
    (root / "docs" / "b.md").write_text(claims)
    (root / "docs" / "a.md").write_text(rest if extracted else claims + rest + "And one line more.\n")
    commit_all(root, "work")

    problems = count_claims.run(Repo.open(root, ("docs/a.md", "docs/b.md"), base.strip()), {})

    assert bool(problems) is fails
    assert not fails or problems[0].startswith("count-claims: docs/b.md has 1, over the 0 it has on the default")
