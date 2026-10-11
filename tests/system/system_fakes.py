from __future__ import annotations

import shutil
import tomllib
from pathlib import Path
from typing import Any

from runtime.shared.settings import Settings, build
from runtime.system import ci_check, steps_prepare
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
    owner = Account("owner", 1000, 1000, tmp_path / "home", ("owner",), "/bin/bash")
    return Install(host, Places.under(tmp_path / "root"), REPO_ROOT, product, "main", owner, dict(GIVEN), healthchecks)


GH = "/usr/bin/gh"
SHA = "0123456789abcdef0123456789abcdef01234567"
PASSING = [("test", "completed", "success"), ("drives", "completed", "success")]


def ci_link(number: int) -> str:
    return f"https://github.com/acme/software-factory/actions/runs/7/job/{number}"


def lookup(name: str) -> str:
    """The command that asks the owner's login shell, started as at a terminal, where it finds ``name``."""
    return f"/bin/bash -lic echo '{steps_prepare.LOOKUP_MARK}'; command -v {name}"


def ci_answers(checkout: Path, sha: str, runs: list[tuple[str, str, str]]) -> dict[str, tuple[int, str]]:
    """What the owner's signed-in gh answers about the factory's CI on ``sha``: each run's name, status and
    conclusion, linked by its place in ``runs``."""
    lines = "".join(f"{name}\t{status}\t{end}\t{ci_link(n)}\n" for n, (name, status, end) in enumerate(runs))
    checks = f"repos/acme/software-factory/commits/{sha}/check-runs?per_page=100"
    return {
        lookup("gh"): (0, GH + "\n"),
        f"readlink -f {GH}": (0, GH + "\n"),
        f"{GH} auth status --hostname github.com": (0, ""),
        f"git -C {checkout} remote get-url origin": (0, FACTORY_ORIGIN + "\n"),
        f"{GH} api --paginate {checks} --jq {ci_check.CHECK_FIELDS}": (0, lines),
    }
