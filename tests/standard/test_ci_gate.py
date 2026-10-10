from __future__ import annotations

from pathlib import Path
from typing import NoReturn

import pytest
from deploy_kit import FACTORY_TOML

from standard.server import ci_gate

SHA = "abcdef1234567"


class Execed(Exception):
    def __init__(self, path: str, argv: list[str]) -> None:
        self.path, self.argv = path, argv


def execv(path: str, argv: list[str]) -> NoReturn:
    raise Execed(path, argv)


@pytest.mark.parametrize(
    ("command", "args"),
    [
        (f"deploy prod {SHA}", ["prod", SHA]),
        (f"deploy staging {SHA} follow", ["staging", SHA, "follow"]),
        (f"deploy staging {SHA} pin feature/new-page", ["staging", SHA, "pin", "feature/new-page"]),
        (f"deploy staging {SHA} pin -", ["staging", SHA, "pin", "-"]),
        (f"deploy prod {SHA}; rm -rf /", None),
        (f"deploy prod {SHA}\nrm -rf /", None),
        (f"deploy prod {SHA}\n", None),
        (f"deploy prod {SHA} follow", None),
        (f"deploy prod  {SHA}", None),
        (f"deploy staging {SHA}", None),
        (f"deploy staging {SHA} follow extra", None),
        (f"deploy staging {SHA} pin", None),
        (f"deploy staging {SHA} pin --help", None),
        (f"deploy staging {SHA} pin a$(id)", None),
        (f"deploy staging {SHA} pin {'a' * 101}", None),
        ("deploy prod ABCDEF1", None),
        ("deploy prod abcdef", None),
        (f"deploy dev {SHA}", None),
        (f"bash -c 'deploy prod {SHA}'", None),
        ("", None),
    ],
)
def test_only_the_three_deploy_shapes_pass_the_gate(tmp_path: Path, command: str, args: list[str] | None) -> None:
    product = tmp_path / "product.toml"
    product.write_text(FACTORY_TOML)
    notes: list[str] = []

    if args is None:
        assert ci_gate.main({"SSH_ORIGINAL_COMMAND": command}, product, execv, notes.append) == 2
        assert notes == [f"refused: {command[:300]!r}"]
        return
    with pytest.raises(Execed) as started:
        ci_gate.main({"SSH_ORIGINAL_COMMAND": command}, product, execv, notes.append)
    assert started.value.path == "/usr/bin/sudo"
    assert started.value.argv == ["sudo", "-n", "-u", "deploy", "/usr/local/bin/factory-deploy", *args]
    assert notes == []
