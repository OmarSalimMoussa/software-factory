from __future__ import annotations

from pathlib import Path

import pytest

from checks.runner import cli


def product(tmp_path: Path, verbs: str, deps: str) -> Path:
    dev = tmp_path / "scripts" / "dev"
    dev.parent.mkdir(parents=True)
    dev.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = verbs ]; then printf "{verbs}"; exit 0; fi\n'
        f'if [ "$1" = deps ]; then {deps}; fi\n'
    )
    dev.chmod(0o755)
    return tmp_path


@pytest.mark.parametrize(
    ("verbs", "deps", "problem", "installed"),
    [
        ("up\\ntest\\ndeps\\n", "touch installed", "", True),
        ("up\\ntest\\n", "touch installed", "", False),
        ("up\\ndeps\\n", "echo no network >&2; exit 3", "scripts/dev deps failed (exit 3)", False),
    ],
    ids=["offered", "not-offered", "fails"],
)
def test_the_products_deps_run_before_any_check(
    tmp_path: Path, verbs: str, deps: str, problem: str, installed: bool
) -> None:
    root = product(tmp_path, verbs, deps)
    found = cli.prepare(root)
    assert found.startswith(problem) and bool(found) == bool(problem)
    assert (root / "installed").exists() == installed
