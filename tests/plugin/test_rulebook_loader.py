from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("rulebook_loader", ROOT / "plugin" / "hooks" / "rulebook_loader.py")
assert _spec is not None and _spec.loader is not None
loader = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(loader)


@pytest.mark.parametrize(
    ("url", "gets_rulebook"),
    [
        ("git@github.com:acme-depot/shop.git", True),
        ("https://github.com/Acme-Depot/shop", True),
        ("ssh://git@github.com:22/acme-depot/shop.git", True),
        # Only the owner segment counts: a fork, a look-alike owner or a repo named after the org gets nothing.
        ("https://github.com/someone/acme-depot.git", False),
        ("git@github.com:acme-depot-mirror/shop.git", False),
        ("https://github.com/acme-depot", False),
        ("/srv/acme-depot/shop", False),
        ("file:///srv/acme-depot/shop", False),
    ],
)
def test_the_rulebook_goes_only_to_repos_the_org_owns(url: str, gets_rulebook: bool) -> None:
    assert loader.in_org_repo([url], ["acme-depot"]) is gets_rulebook
