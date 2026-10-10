from __future__ import annotations

import shutil
import tomllib
from pathlib import Path
from typing import Any

from runtime.shared.settings import Settings, build
from runtime.system.host import Account, DryHost
from runtime.system.job import REPO_ROOT, Install

FIXTURE = REPO_ROOT / "testdata" / "fixture-product"

# What only the owner can give with --set; the install derives every repo.* setting itself.
GIVEN: dict[str, Any] = {}
PRODUCT_ORIGIN = "git@github.com:acme/shop.git"
FACTORY_ORIGIN = "git@github.com:acme/software-factory.git"


def settings(**changes: Any) -> Settings:
    defaults = tomllib.loads((REPO_ROOT / "settings.toml").read_text(encoding="utf-8"))
    derived = {"repo.github": "acme/shop", "repo.path": "/home/software-agent/shop"}
    return build(defaults, {**GIVEN, **derived, **changes})


def dry_job(tmp_path: Path, *, healthchecks: bool = False) -> Install:
    from runtime.shared.paths import Places

    product = tmp_path / "shop"
    product.mkdir(exist_ok=True)
    shutil.copy(FIXTURE / "factory.toml", product / "factory.toml")
    host = DryHost(
        answers={
            f"git -C {REPO_ROOT} remote get-url origin": (0, FACTORY_ORIGIN),
            f"git -C {product} remote get-url origin": (0, PRODUCT_ORIGIN),
        }
    )
    owner = Account("owner", 1000, 1000, tmp_path / "home", ("owner",))
    return Install(host, Places.under(tmp_path / "root"), REPO_ROOT, product, "main", owner, dict(GIVEN), healthchecks)
