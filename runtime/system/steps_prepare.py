"""The steps before anything on the machine changes: check it, refuse while work is live, archive and test the
commit, settle the settings, then stop the running factory."""

from __future__ import annotations

import json
import tomllib
from typing import Any

from runtime.shared.models import DEPRIORITIZED, RETIRED
from runtime.shared.settings import SettingsError, _flatten, build
from runtime.system import layout
from runtime.system.host import CommandError
from runtime.system.job import Install, github_name, refuse
from runtime.warden import registry

_MACHINE_TOOLS = ("git", "tar", "systemctl", "systemd-run", "runuser", "loginctl", "useradd", "nft", "tailscale")
_OWNER_TOOLS = ("uv", "claude")
# A session in any other state is working, waiting or between the two: reinstalling under it would lose its work.
_RESTING = (DEPRIORITIZED, RETIRED)


def owner_tool(ctx: Install, name: str) -> tuple[int, str]:
    """The file the owner's login shell runs for ``name``, every link followed, kept for ``ctx.tool``; the shell's exit
    code beside it."""
    code, out = ctx.host.query(["sh", "-lc", f'readlink -f "$(command -v {name})"'], user=ctx.owner.name)
    ctx.tools[name] = out.strip()
    return code, out.strip()


def check_machine(ctx: Install) -> None:
    host, owner = ctx.host, ctx.owner.name
    problems = [f"{tool} is not installed" for tool in _MACHINE_TOOLS if host.query(["which", tool])[0] != 0]
    for tool in _OWNER_TOOLS:
        code, found = owner_tool(ctx, tool)
        if (code != 0 or not found) and not host.dry:
            problems.append(f"{tool} is not on {owner}'s PATH")
    for name, path in (("factory", ctx.factory), ("product", ctx.product)):
        code, out = host.query(["git", "-C", str(path), "remote", "get-url", "origin"], user=owner)
        if code != 0 and not host.dry:
            problems.append(f"the {name} checkout {path} has no origin remote")
        setattr(ctx, f"{name}_origin", out.strip())
    mine, theirs = github_name(ctx.factory_origin), github_name(ctx.product_origin)
    if ctx.factory.resolve() == ctx.product.resolve() or (mine is not None and mine == theirs):
        problems.append("--repo is the factory's own repo: the factory never works on itself")
    if problems:
        raise refuse("; ".join(problems))


def refuse_live_sessions(ctx: Install) -> None:
    """Plan R5: a reinstall waits until every session is deprioritized or retired (Pause the factory does that)."""
    ctx.first = not ctx.host.exists(layout.install_record(ctx.places.state))
    try:
        sessions = registry.read(ctx.places)
    except PermissionError:
        if not ctx.host.dry:
            raise
        ctx.host.say("  (a dry run as you cannot read the registry; the install checks it as root)")
        return
    live = sorted(s.name for s in sessions.values() if s.state not in _RESTING)
    if live:
        raise refuse(f"{len(live)} session(s) still working or waiting ({', '.join(live)}): pause the factory first")


def archive(ctx: Install) -> None:
    host, owner = ctx.host, ctx.owner.name
    if ctx.stage is not None:
        host.say(f"  {ctx.sha[:12]} is archived in {ctx.stage}, and this install runs from it")
        return
    wanted = f"{ctx.commit}^{{commit}}"
    code, out = host.query(["git", "-C", str(ctx.factory), "rev-parse", "--verify", wanted], user=owner)
    if code != 0 and not host.dry:
        raise refuse(f"{ctx.commit} is not a commit in {ctx.factory}")
    ctx.sha = out.strip() or f"<sha of {ctx.commit}>"
    stage = host.scratch(owner)
    ctx.stage = stage
    host.run(["git", "-C", str(ctx.factory), "archive", "--format=tar", "-o", str(stage / "factory.tar"), ctx.sha],
             user=owner)  # fmt: skip
    host.run(["mkdir", str(stage / "tree")], user=owner)
    host.run(["tar", "-x", "-f", str(stage / "factory.tar"), "-C", str(stage / "tree")], user=owner)


def test_copy(ctx: Install) -> None:
    try:
        ctx.host.run(
            [ctx.tool("uv"), "run", "--frozen", "--extra", "dev", "pytest", "-q", "-p", "no:cacheprovider"],
            user=ctx.owner.name, cwd=ctx.tree(),
        )  # fmt: skip
    except CommandError as exc:
        tail = exc.output[-400:]
        raise refuse(f"the factory's tests failed on {ctx.sha[:12]}, so nothing was installed: {tail}") from exc


def _saved_overrides(ctx: Install) -> dict[str, Any]:
    try:
        text = ctx.host.read(ctx.places.overrides)
    except FileNotFoundError:
        return {}
    except PermissionError:
        if not ctx.host.dry:
            raise
        ctx.host.say("  (a dry run as you cannot read the saved settings; the install reads them as root)")
        return {}
    try:
        return _flatten(tomllib.loads(text))
    except tomllib.TOMLDecodeError as exc:
        raise refuse(f"{ctx.places.overrides} is not TOML ({exc}): fix it by hand, then install again") from exc


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
        raise refuse(f"tailscale status names no machine ({type(exc).__name__}): sign in to tailscale") from exc
    if not isinstance(name, str) or not name.strip("."):
        raise refuse("tailscale names no tailnet for this machine: sign in to tailscale, then install again")
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
        raise refuse(f"the settings cannot be used: {problems} (set each with --set key=value)") from exc
    ctx.overrides = overrides
    if not str(ctx.settings.get_str("repo.path")).startswith(f"{layout.AGENT_HOME}/"):
        raise refuse(f"repo.path must be inside {layout.AGENT_HOME}, the agent user's home")


def stop_factory(ctx: Install) -> None:
    """Stop the warden, the dashboard and the agents' daemon, then check the registry again with the warden down."""
    running = [unit for unit in layout.SERVICES if ctx.host.exists(layout.UNITS_DIR / unit)]
    if running:
        ctx.host.run(["systemctl", "stop", *running])
    else:
        ctx.host.say("  nothing is installed yet")
    refuse_live_sessions(ctx)
