"""The GitHub side of the install, done with the owner's own `gh` login when it has one: the warden's deploy key on the
product, the read-only key the product's CI checks the factory out with, and that key's private half as the product's
secret. Each key replaces the one of the same title, so a repo never holds a stale one. Without a signed-in `gh` the
install says what to add by hand instead.
"""

from __future__ import annotations

import json
from pathlib import Path

from runtime.system import steps_prepare
from runtime.system.host import Host
from runtime.system.job import Install

WARDEN_TITLE = "software-factory warden"
READ_SECRET = "FACTORY_READ_KEY"


def read_title(product: str) -> str:
    return f"{product} CI (read-only)"


def owner_gh(host: Host, owner: str) -> str:
    """The owner's `gh`, when it is installed and signed in to github.com; "" otherwise."""
    code, found = steps_prepare.login_tool(host, owner, "gh")
    if code != 0 or not found:
        return ""
    status, _ = host.query([found, "auth", "status", "--hostname", "github.com"], user=owner)
    return found if status == 0 else ""


def put_deploy_key(ctx: Install, gh: str, repo: str, title: str, public: str, *, write: bool) -> str:
    """Make ``public`` the one deploy key titled ``title`` on ``repo``; what was done, in a few words."""
    host, owner = ctx.host, ctx.owner.name
    key = " ".join(public.split()[:2])  # GitHub keeps a key without its comment
    listed = host.run([gh, "api", f"repos/{repo}/keys"], user=owner)
    keys = json.loads(listed) if listed.strip() else []
    present = [k for k in keys if isinstance(k, dict) and str(k.get("key", "")).strip() == key]
    if not present:
        flag = "false" if write else "true"
        host.run([gh, "api", "-X", "POST", f"repos/{repo}/keys", "-f", f"title={title}", "-f", f"key={key}",
                  "-F", f"read_only={flag}"], user=owner)  # fmt: skip
    stale = [k for k in keys if isinstance(k, dict) and k.get("title") == title and k not in present]
    for old in stale:
        host.run([gh, "api", "-X", "DELETE", f"repos/{repo}/keys/{old['id']}"], user=owner)
    said = "already there" if present else "added"
    return said + (f", {len(stale)} older one(s) of that title removed" if stale else "")


def put_secret(ctx: Install, gh: str, repo: str, name: str, private_key: Path) -> None:
    """Save the file's text as ``repo``'s secret ``name``, through stdin: it is never on a command line or printed."""
    value = ctx.host.read(private_key)
    ctx.host.run([gh, "secret", "set", name, "--repo", repo], user=ctx.owner.name, stdin=value)
