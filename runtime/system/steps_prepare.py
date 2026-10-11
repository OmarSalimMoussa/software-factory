"""The steps before anything on the machine changes: check it, refuse while work is live, run from the commit's
sealed copy, settle the settings, then stop the running factory."""

from __future__ import annotations

import json
import tomllib
from typing import Any

from runtime.shared.models import DEPRIORITIZED, RETIRED
from runtime.shared.settings import SettingsError, _flatten, build
from runtime.system import layout
from runtime.system.host import Host
from runtime.system.job import Install, github_name, github_origin, refuse
from runtime.warden import registry

# What the owner sets up before an install; the packages step installs the rest (nftables among them).
_MACHINE_TOOLS = ("git", "tar", "systemctl", "systemd-run", "runuser", "loginctl", "useradd", "tailscale")
_OWNER_TOOLS = ("uv", "claude")
# A session in any other state is working, waiting or between the two: reinstalling under it would lose its work.
_RESTING = (DEPRIORITIZED, RETIRED)


def login_tool(host: Host, user: str, name: str) -> tuple[int, str]:
    """The file ``user``'s login shell runs for ``name``, every link followed; the shell's exit code beside it."""
    code, out = host.query(["sh", "-lc", f'readlink -f "$(command -v {name})"'], user=user)
    return code, out.strip()


def owner_tool(ctx: Install, name: str) -> tuple[int, str]:
    """The owner's ``name`` (``login_tool``), kept for ``ctx.tool``."""
    code, found = login_tool(ctx.host, ctx.owner.name, name)
    ctx.tools[name] = found
    return code, found


def check_machine(ctx: Install) -> None:
    host, owner = ctx.host, ctx.owner.name
    problems = [f"{tool} is not installed" for tool in _MACHINE_TOOLS if host.query(["which", tool])[0] != 0]
    for tool in _OWNER_TOOLS:
        code, found = owner_tool(ctx, tool)
        if (code != 0 or not found) and not host.dry:
            problems.append(f"{tool} is not installed for you ({owner})")
    checkouts = {"product": ctx.product} if ctx.factory is None else {"factory": ctx.factory, "product": ctx.product}
    for name, path in checkouts.items():
        code, out = host.query(["git", "-C", str(path), "remote", "get-url", "origin"], user=owner)
        if code != 0 and not host.dry:
            problems.append(f"{path} is not a clone of the {name}'s repository on GitHub")
        setattr(ctx, f"{name}_origin", out.strip())
    if ctx.release is not None:
        ctx.factory_origin = github_origin(str(ctx.release["repo"]))
    mine, theirs = github_name(ctx.factory_origin), github_name(ctx.product_origin)
    if (ctx.factory is not None and ctx.factory.resolve() == ctx.product.resolve()) or (mine and mine == theirs):
        problems.append("the product checkout you named is the factory's own repository, and the factory never works "
                        "on itself")  # fmt: skip
    if problems:
        raise refuse(f"This computer is not ready for the factory: {'; '.join(problems)}. Fix that, then install "
                     "again")  # fmt: skip


def refuse_live_sessions(ctx: Install) -> None:
    """Plan R5: a reinstall waits until every session is deprioritized or retired (Pause the factory does that)."""
    ctx.first = not ctx.host.exists(layout.install_record(ctx.places.state))
    try:
        sessions = registry.read(ctx.places)
    except PermissionError:
        if not ctx.host.dry:
            raise
        ctx.host.say("(a dry run as you cannot read the registry; the install checks it as root)")
        return
    live = sorted(s.name for s in sessions.values() if s.state not in _RESTING)
    if live:
        raise refuse(f"Pause the factory on the dashboard first, then try again: {len(live)} of its agents are still "
                     f"working or waiting ({', '.join(live)})")  # fmt: skip


def sealed_copy(ctx: Install) -> None:
    """The commit is sealed before the first step (runtime/system/sealed.py); a dry run says what would be."""
    host = ctx.host
    if ctx.release is not None:
        host.say(f"{ctx.sha[:12]} is sealed from the release built {ctx.release['built']}, root-owned and read-only, "
                 f"in {ctx.stage}; every step runs from it")  # fmt: skip
        return
    git = ["git", "-C", str(ctx.factory)]
    if ctx.stage is not None:
        ctx.subject = host.query([*git, "log", "-1", "--format=%s", ctx.sha, "--"], user=ctx.owner.name)[1].strip()
        host.say(f"{ctx.sha[:12]} is sealed, root-owned and read-only, in {ctx.stage}; every step runs from it")
        return
    if not host.dry:
        raise refuse("this install does not run from a sealed copy of its commit: start it with install.py")
    out = host.query([*git, "rev-parse", "--verify", f"{ctx.commit}^{{commit}}"], user=ctx.owner.name)[1]
    ctx.sha = out.strip() or f"<sha of {ctx.commit}>"
    host.say(f"would archive {ctx.sha[:12]} as {ctx.owner.name}, unpack it as root into a new folder in "
             f"{layout.STAGE_ROOT}, make it read-only, and run every step from that copy")  # fmt: skip


def _saved_overrides(ctx: Install) -> dict[str, Any]:
    try:
        text = ctx.host.read(ctx.places.overrides)
    except FileNotFoundError:
        return {}
    except PermissionError:
        if not ctx.host.dry:
            raise
        ctx.host.say("(a dry run as you cannot read the saved settings; the install reads them as root)")
        return {}
    try:
        return _flatten(tomllib.loads(text))
    except tomllib.TOMLDecodeError as exc:
        raise refuse(
            f"Your saved settings in {ctx.places.overrides} cannot be read ({exc}): fix the file by hand, "
            "then install again"
        ) from exc


def _effective(defaults: dict[str, Any], overrides: dict[str, Any], key: str) -> Any:
    if key in overrides:
        return overrides[key]
    node: Any = defaults
    for part in key.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    return node


def _tailnet_name(ctx: Install) -> str | None:
    code, out = ctx.host.query(["tailscale", "status", "--json"])
    if ctx.host.dry and not out:
        return None
    try:
        name = json.loads(out)["Self"]["DNSName"] if code == 0 else ""
    except (ValueError, KeyError, TypeError) as exc:
        raise refuse(
            "Tailscale is not signed in on this computer, so the dashboard has no address: sign in to "
            "Tailscale, then install again"
        ) from exc
    if not isinstance(name, str) or not name.strip("."):
        raise refuse(
            "Tailscale is not signed in on this computer, so the dashboard has no address: sign in to "
            "Tailscale, then install again"
        )
    return name.strip(".")


def settle_settings(ctx: Install) -> None:
    """The commit's defaults, the owner's saved overrides and --set, plus what only the install knows; checked."""
    defaults = tomllib.loads((ctx.tree() / "settings.toml").read_text(encoding="utf-8"))
    overrides = {**_saved_overrides(ctx), **ctx.changes}
    derived = {
        "repo.path": str(layout.AGENT_HOME / ctx.product.name),
        "repo.github": github_name(ctx.product_origin) or "",
        "factory.clone": str(ctx.places.state / "factory-repo"),
    }
    for key, value in derived.items():
        if not _effective(defaults, overrides, key) and value:
            overrides[key] = value
    tailnet = _tailnet_name(ctx)
    hosts = list(_effective(defaults, overrides, "dashboard.hosts") or [])
    if tailnet and tailnet not in hosts:
        overrides["dashboard.hosts"] = [*hosts, tailnet]
    try:
        ctx.settings = build(defaults, overrides)
    except SettingsError as exc:
        problems = "; ".join(exc.problems)
        raise refuse(
            f"The settings cannot be used: {problems}. Set each with --set key=value, then install again"
        ) from exc
    ctx.overrides = overrides
    if not str(ctx.settings.get_str("repo.path")).startswith(f"{layout.AGENT_HOME}/"):
        raise refuse(f"The setting repo.path must be a folder inside {layout.AGENT_HOME}, the agents' home: set it "
                     "with --set repo.path=..., then install again")  # fmt: skip


def stop_factory(ctx: Install) -> None:
    """Stop the warden, the dashboard and the agents' daemon, then check the registry again with the warden down."""
    running = [unit for unit in layout.SERVICES if ctx.host.exists(layout.UNITS_DIR / unit)]
    if running:
        ctx.host.run(["systemctl", "stop", *running])
    else:
        ctx.host.say("nothing is installed yet")
    refuse_live_sessions(ctx)
