"""One install: what the owner asked for, and what the steps learn as they run.

The steps fill it in order (the commit's sha, the archived tree, the settled settings), and each later step asks
for what it needs through a method that fails loudly when an earlier step did not run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from runtime.shared.paths import Places
from runtime.shared.protocol import Refused
from runtime.shared.settings import Settings
from runtime.system.host import Account, Host

RULE = "install"
REPO_ROOT = Path(__file__).resolve().parents[2]
# owner/name from the remote forms GitHub hands out: https, ssh with scp syntax, and ssh:// URLs.
_GITHUB = re.compile(r"github\.com[:/]+([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")


def refuse(reason: str) -> Refused:
    return Refused(RULE, reason)


def github_name(url: str) -> str | None:
    found = _GITHUB.search(url.strip())
    return f"{found.group(1)}/{found.group(2)}".lower() if found else None


@dataclass
class Install:
    host: Host
    places: Places
    factory: Path  # the owner's clone of the factory; the commit is archived from it
    product: Path  # the owner's checkout of the product (--repo)
    commit: str
    owner: Account
    changes: dict[str, Any]  # --set key=value, applied over the saved overrides
    healthchecks: bool
    sha: str = ""
    stage: Path | None = None
    settings: Settings | None = None
    overrides: dict[str, Any] = field(default_factory=dict)
    first: bool = True  # no earlier install recorded
    reached: int = 0  # the number of the step running, or the last one done
    product_origin: str = ""
    factory_origin: str = ""
    tools: dict[str, str] = field(default_factory=dict)  # the owner's uv and claude
    drilled: list[dict[str, object]] = field(default_factory=list)  # each drill's name, verdict and evidence

    def tree(self) -> Path:
        """The factory's files at the commit; a dry run reads the copy it runs from."""
        if self.host.dry:
            return REPO_ROOT
        if self.stage is None:
            raise refuse("the commit was not archived before this step")
        return self.stage / "tree"

    def templates(self) -> Path:
        return self.tree() / "runtime" / "system"

    def need_settings(self) -> Settings:
        if self.settings is None:
            raise refuse("the settings were not settled before this step")
        return self.settings

    def checkout(self) -> Path:
        """The agent user's own checkout of the product, where every session works."""
        return Path(self.need_settings().get_str("repo.path"))

    def uid(self, name: str) -> str:
        account = self.host.account(name)
        if account is not None:
            return str(account.uid)
        if self.host.dry:
            return f"<uid of {name}>"
        raise refuse(f"there is no user {name}")

    def tool(self, name: str) -> str:
        return self.tools.get(name) or f"<{name}>"
