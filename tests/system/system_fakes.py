from __future__ import annotations

import shutil
import tomllib
from pathlib import Path
from typing import Any

from runtime.shared.settings import Settings, build
from runtime.system.host import Account, DryHost
from runtime.system.install_steps import steps_prepare
from runtime.system.job import REPO_ROOT, Install
from standard.server import edge
from standard.services import provision, stack

FIXTURE = REPO_ROOT / "testdata" / "fixture-product"

# What only the owner can give with --set; the install derives every repo.* setting itself.
PUBLIC_HOST = "factory.example.com"
GIVEN: dict[str, Any] = {"dashboard.hosts": ["127.0.0.1", "localhost", PUBLIC_HOST]}
PRODUCT_ORIGIN = "git@github.com:acme/shop.git"
FACTORY_ORIGIN = "git@github.com:acme/software-factory.git"
INSPECT = " ".join(["docker", "inspect", "--format", "{{.Name}} {{.State.Running}}", *provision.SHARED])


def settings(**changes: Any) -> Settings:
    defaults = tomllib.loads((REPO_ROOT / "settings.toml").read_text(encoding="utf-8"))
    derived = {"repo.github": "acme/shop", "repo.path": "/home/software-agent/shop"}
    return build(defaults, {**GIVEN, **derived, **changes})


def dry_job(tmp_path: Path, *, healthchecks: bool = False) -> Install:
    from runtime.shared.paths import Places

    product = tmp_path / "shop"
    product.mkdir(exist_ok=True)
    shutil.copy(FIXTURE / "factory.toml", product / "factory.toml")
    places = Places.under(tmp_path / "root")
    host = DryHost(
        paths={edge.SITES, places.services / stack.ENV_FILE},
        answers={
            f"git -C {REPO_ROOT} remote get-url origin": (0, FACTORY_ORIGIN),
            f"git -C {product} remote get-url origin": (0, PRODUCT_ORIGIN),
            INSPECT: (0, "".join(f"/{name} true\n" for name in provision.SHARED)),
        },
    )
    owner = Account("owner", 1000, 1000, tmp_path / "home", ("owner",), "/bin/bash")
    return Install(host, places, REPO_ROOT, product, "main", owner, dict(GIVEN), healthchecks)


SHA = "0123456789abcdef0123456789abcdef01234567"


def lookup(name: str) -> str:
    """The command that asks the owner's shell, started as a terminal window starts it, where it finds ``name``."""
    return " ".join(steps_prepare.lookup_argv("/bin/bash", name))


def found_at(path: str) -> str:
    """What that shell prints when it finds the tool at ``path``."""
    return f"{steps_prepare.LOOKUP_MARK}\n{path}\n{steps_prepare.LOOKUP_END}\n"
