"""The protected paths are refused at every door: the guard's write rule and the git hook's commit check. They are
the fixed set, factory.toml and every script it names (read from the main checkout), and the product's own
(repo.protected_paths). While that factory.toml cannot be read, every door fails closed."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from guard_kit import Kit

from runtime.guard import commit_check
from runtime.guard.commit_check import refusal
from runtime.guard.facts import read_settings
from runtime.guard.writes import protected_paths


@pytest.mark.parametrize(
    ("path", "refused"),
    [
        ("scripts/deploy.sh", True),
        ("deploy/prod/run.sh", True),
        ("factory.toml", True),
        ("scripts/smoke-check", True),
        ("GOAL.md", True),
        (".claude/settings.json", False),
        (".mcp.json", False),
        ("scripts/deploy.sh.bak", False),
        ("scripts/smoke-check.md", False),
        ("src/deploy/app.py", False),
    ],
)
@pytest.mark.parametrize("door", ["guard", "hook"])
def test_a_protected_path_is_refused_at_every_door(tmp_path: Path, door: str, path: str, refused: bool) -> None:
    kit = Kit(tmp_path)
    kit.session("implementer")
    settings = read_settings(kit.places)
    if door == "guard":
        found = kit.rule("Write", {"file_path": str(kit.worktree / path), "content": "x"}) == "protected-paths"
    else:
        prefixes = protected_paths(settings)
        found = bool(refusal([path], prefixes=prefixes, in_slice=True, hook="pre-commit", on_default=False,
                             merging=False, factory=False))  # fmt: skip
    assert found is refused


@pytest.mark.parametrize("damage", ["missing", "not toml", "breaks the schema"])
def test_an_unreadable_factory_toml_fails_closed_at_every_door(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], damage: str
) -> None:
    kit = Kit(tmp_path)
    kit.session("implementer")
    toml = kit.repo / "factory.toml"
    if damage == "missing":
        toml.unlink()
    else:
        toml.write_text("[product\n" if damage == "not toml" else toml.read_text() + "\n[surprise]\n")
    settings = read_settings(kit.places)

    assert kit.rule("Write", {"file_path": str(kit.worktree / "src" / "app.py"), "content": "x"}) == "protected-paths"

    product = tmp_path / "commit"
    product.mkdir()
    for args in (["init", "-q"], ["add", "app.py"]):
        (product / "app.py").write_text("x = 1\n")
        subprocess.run(["git", *args], cwd=product, check=True)
    monkeypatch.chdir(product)
    monkeypatch.setattr(commit_check, "read_settings", lambda places: settings)
    monkeypatch.setattr(commit_check, "_in_slice", lambda: True)
    assert commit_check.main(["pre-commit"]) == 1
    assert "factory.toml cannot be read" in capsys.readouterr().err
