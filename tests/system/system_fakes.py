from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from runtime.shared.settings import Settings, build
from runtime.system.host import Account, DryHost
from runtime.system.job import REPO_ROOT, Install

# What only the owner can give; the install derives the rest of repo.* itself.
GIVEN = {
    "repo.staging_health": "https://staging.example.test/health",
    "repo.prod_health": "https://example.test/health",
    "repo.owner_login": "the-owner",
}
PRODUCT_ORIGIN = "git@github.com:acme/shop.git"
FACTORY_ORIGIN = "git@github.com:acme/software-factory.git"


def settings(**changes: Any) -> Settings:
    defaults = tomllib.loads((REPO_ROOT / "settings.toml").read_text(encoding="utf-8"))
    derived = {"repo.github": "acme/shop", "repo.path": "/home/software-agent/shop"}
    return build(defaults, {**GIVEN, **derived, **changes})


def dry_job(tmp_path: Path, *, healthchecks: bool = False) -> Install:
    from runtime.shared.paths import Places

    product = tmp_path / "shop"
    host = DryHost(
        answers={
            f"git -C {REPO_ROOT} remote get-url origin": (0, FACTORY_ORIGIN),
            f"git -C {product} remote get-url origin": (0, PRODUCT_ORIGIN),
        }
    )
    owner = Account("owner", 1000, 1000, tmp_path / "home", ("owner",))
    return Install(host, Places.under(tmp_path / "root"), REPO_ROOT, product, "main", owner, dict(GIVEN), healthchecks)
