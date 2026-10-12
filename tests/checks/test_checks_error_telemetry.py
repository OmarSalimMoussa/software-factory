from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from checks.runner.repo import Repo
from checks.runner.values import ValuesError
from checks.standard import error_telemetry

MakeRepo = Callable[[dict[str, str]], Path]
BASE: dict[str, Any] = {
    "python_roots": ["."],
    "first_party": ["app"],
    "excluded": {},
    "test_support_dirs": [],
    "scanned_testdata": [],
    "vendored": [],
    "vendored_prefix": "LOCAL:",
    "typescript_from": "",
    "quiet_calls": [],
    "floating_entries": [],
    "floating_components": [],
    "vite_configs": [],
    "components": [
        {
            "prefix": "app/",
            "name": "app",
            "sink": "sdk-logging",
            "grounds": "the app reports through stdlib logging",
            "reporters": [],
            "seams": [],
            "log_modules": [],
        },
        {
            "prefix": "scripts/",
            "name": "dev-cli",
            "sink": "exit",
            "grounds": "scripts run where stderr and the exit code are seen",
            "reporters": [],
            "seams": [],
            "log_modules": [],
        },
    ],
    "processes": {},
    "non_booting": {},
    "sdk_owners": {},
    "entry_owns_runtime": {},
    "dist_to_src": {},
}


def check(root: Path, **changes: Any) -> list[str]:
    values = copy.deepcopy(BASE) | changes
    return error_telemetry.run(Repo.open(root), values)


def handler(body: str, marker: str = "") -> str:
    return (
        "import logging\n\nlogger = logging.getLogger(__name__)\n\n\ndef load() -> None:\n"
        f"    try:\n        open('x')\n    except OSError:{marker}\n        {body}\n"
    )


@pytest.mark.parametrize(
    ("body", "kind"),
    [
        ("pass", "silent"),
        ("logger.info('gone')", "quiet-log"),
        ("logger.warning('gone')", None),
        ("raise", None),
        ("report()", None),  # a local helper that reports is followed
    ],
)
def test_a_catch_site_must_raise_or_report(make_repo: MakeRepo, body: str, kind: str | None) -> None:
    helper = "\n\ndef report() -> None:\n    logger.error('the file is gone')\n"
    root = make_repo({"app/store.py": handler(body) + helper})
    problems = check(root)
    if kind is None:
        assert problems == []
    else:
        assert len(problems) == 1 and f"app/store.py:9 — {kind}:" in problems[0]


@pytest.mark.parametrize(
    ("body", "marker"),
    [
        ("pass", "  # telemetry: expected — a missing cache file is rebuilt on the next start"),
        ("outcome = 'missing'", "  # telemetry: surfaced — the caller shows `outcome` to the person"),
    ],
)
def test_a_valid_marker_clears_the_site(make_repo: MakeRepo, body: str, marker: str) -> None:
    assert check(make_repo({"app/store.py": handler(body, marker)})) == []


@pytest.mark.parametrize(
    ("body", "marker", "fragment"),
    [
        ("raise", "  # telemetry: expected — a missing cache file is rebuilt on start", "stale: the site already"),
        ("pass", "  # telemetry: expected — fine", "is generic"),
        ("pass", "  # telemetry: expected — rebuilt on start", "too short"),
        ("pass", "  # telemetry: expected — a missing cache file, fix later", 'says "later"'),
        ("pass", "  # telemetry: surfaced — the caller reads `outcome` afterwards", "`surfaced` must name"),
        ("pass", "  # LOCAL: telemetry: expected — a missing cache file is rebuilt on start", "vendored files only"),
    ],
)
def test_a_weak_or_misplaced_marker_fails(make_repo: MakeRepo, body: str, marker: str, fragment: str) -> None:
    problems = check(make_repo({"app/store.py": handler(body, marker)}))
    assert any(fragment in p for p in problems), problems


def test_a_marker_on_no_site_is_stale(make_repo: MakeRepo) -> None:
    root = make_repo({"app/a.py": "# telemetry: expected — nothing here catches anything at all\nx = 1\n"})
    assert check(root) == ["error-telemetry: marker at app/a.py:1 — stale: it sits on no catch site; remove it"]


def test_a_file_no_component_claims_fails(make_repo: MakeRepo) -> None:
    root = make_repo({"lib/a.py": "x = 1\n", "lib/a_test.py": "x = 1\n"})
    (problem,) = check(root)
    assert problem.startswith("error-telemetry: no component row for lib/a.py — add a components row")


def test_a_suffixless_shell_script_is_read_by_its_shebang(make_repo: MakeRepo) -> None:
    script = "#!/usr/bin/env bash\nset -euo pipefail\nrm -f cache || true\n"
    problems = check(make_repo({"scripts/clean*": script}))
    assert len(problems) == 1 and "scripts/clean:3 — shell-swallow: `|| true`" in problems[0]


@pytest.mark.parametrize(
    ("non_booting", "expected"),
    [
        ({}, "entrypoint scripts/tool.py has no processes or non_booting row"),
        ({"scripts/tool.py": "run by hand", "scripts/old.py": "gone"}, "stale non_booting row scripts/old.py"),
    ],
)
def test_every_entrypoint_has_exactly_its_row(make_repo: MakeRepo, non_booting: dict[str, str], expected: str) -> None:
    root = make_repo({"scripts/tool.py": "if __name__ == '__main__':\n    print('hi')\n"})
    (problem,) = check(root, non_booting=non_booting)
    assert expected in problem


def test_a_booting_process_must_make_its_boot_call(make_repo: MakeRepo) -> None:
    root = make_repo({"app/main.py": "def boot_telemetry(x: str) -> None: ...\n\nif __name__ == '__main__':\n"
                      "    boot_telemetry('web')\n"})  # fmt: skip
    rows = {"app/main.py": {"process": "app", "boot": "boot_telemetry('worker')"}}
    (problem,) = check(root, processes=rows)
    assert "app/main.py boots the app process but never calls boot_telemetry('worker')" in problem


@pytest.mark.parametrize(
    ("typescript_from", "expected"),
    [
        ("", "typescript_from in check-values/error-telemetry.toml names no folder"),
        # The pinned version, not the newest: `npm install typescript` would rewrite the folder's package.json.
        ("tools", "install the version its package.json pins (npm ci there)"),
    ],
    ids=["no-folder", "not-installed"],
)
def test_a_ts_file_with_no_typescript_to_load_fails_loudly(
    make_repo: MakeRepo, typescript_from: str, expected: str
) -> None:
    root = make_repo({"app/page.ts": "export const x = 1;\n"})
    (problem,) = check(root, typescript_from=typescript_from)
    assert expected in problem


def test_values_missing_a_key_are_refused(make_repo: MakeRepo) -> None:
    values = "\n".join(f"{key} = []" for key in ("python_roots", "first_party"))
    root = make_repo({"check-values/error-telemetry.toml": values + "\n"})
    with pytest.raises(ValuesError) as caught:
        Repo.open(root).values(error_telemetry.VALUES_FILE, error_telemetry.VALUES)
    assert "check-values/error-telemetry.toml is missing components" in caught.value.problems
