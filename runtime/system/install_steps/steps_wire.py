"""The steps that wire the factory in: managed settings, units, polkit, the firewall, the factory command, the git
hooks, the dashboard's and staging's sites on the box's edge, starting it, and the record a finished install leaves."""

from __future__ import annotations

import json
from pathlib import Path

from runtime.shared import network, upgrades
from runtime.shared.paths import AGENT_USER, MANAGED_SETTINGS, SCRATCH, SLICE, WARDEN_USER
from runtime.system import layout, render
from runtime.system.host import CommandError, Host
from runtime.system.job import Install, github_name, refuse
from standard.server import edge as server_edge


def managed_settings(ctx: Install) -> None:
    text = render.managed_settings(ctx.places, ctx.need_settings(), ctx.templates())
    host = ctx.host
    if ctx.first and host.exists(MANAGED_SETTINGS) and host.read(MANAGED_SETTINGS) != text:
        raise refuse(f"This computer already has Claude Code settings for every user, in {MANAGED_SETTINGS}, and the "
                     "factory needs that file for its agents: move it aside, then install again")  # fmt: skip
    host.mkdir(MANAGED_SETTINGS.parent, mode=0o755)
    host.write(MANAGED_SETTINGS, text, mode=render.MANAGED.mode)


def given(ctx: Install) -> dict[str, str]:
    """The values every rendered artifact is filled from."""
    return render.values(ctx.places, ctx.need_settings(), agent_uid=ctx.uid(AGENT_USER), owner_user=ctx.owner.name)


def units(ctx: Install) -> None:
    values = given(ctx)
    for item in render.UNIT_FILES:
        ctx.host.write(item.dest, render.artifact(item, ctx.templates(), values), mode=item.mode)
    # The warden's run-time budget changes live here; the slice just written already holds the settled values.
    control = layout.SLICE_CONTROL / f"{SLICE}.d"
    if ctx.host.exists(control):
        ctx.host.remove(control)
    ctx.host.run(["systemctl", "daemon-reload"])


def stand_in(ctx: Install) -> None:
    """Start the dashboard's stand-in, which stops the dashboard. It runs from the sealed copy of this commit, which
    the factory installed now may predate; the units step points it at the install."""
    item = next(a for a in render.UNIT_FILES if a.dest.name == layout.UPGRADING_UNIT)
    values = {**given(ctx), "install": str(ctx.tree())}
    ctx.host.write(item.dest, render.artifact(item, ctx.templates(), values), mode=item.mode)
    ctx.host.run(["systemctl", "daemon-reload"])
    ctx.host.run(["systemctl", "start", layout.UPGRADING_UNIT])


def polkit(ctx: Install) -> None:
    text = render.artifact(render.POLKIT, ctx.templates(), given(ctx))
    ctx.host.write(render.POLKIT.dest, text, mode=render.POLKIT.mode)


def firewall(ctx: Install) -> None:
    text = render.artifact(render.FIREWALL, ctx.templates(), given(ctx))
    ctx.host.write(render.FIREWALL.dest, text, mode=render.FIREWALL.mode)
    if ctx.host.dry:
        ctx.host.say("would check the rules with nft before they load")  # a dry run wrote no file to check
        return
    code, _ = ctx.host.query(["nft", "-c", "-f", str(render.FIREWALL.dest)])
    if code != 0:
        raise refuse("This computer's firewall refuses the factory's rules: install again; if it stops here again, "
                     "the log says why")  # fmt: skip


def factory_command(ctx: Install) -> None:
    script = f'#!/bin/sh\nexec {layout.PYTHON} -I {ctx.places.install}/runtime/cli/factory "$@"\n'
    ctx.host.write(layout.FACTORY_BIN, script, mode=0o755)


def git_hooks(ctx: Install) -> None:
    """Both checkouts of the product take their hooks from the install, which no agent can write: the agent's, which
    the vitals watch, and the owner's own, so a merge there into the default branch also needs a tree that passed
    `factory check`."""
    hooks = ctx.places.install / "checks" / "git-hooks"
    ctx.host.run(["git", "-C", str(ctx.checkout()), "config", "core.hooksPath", str(hooks)], user=AGENT_USER)
    ctx.host.run(["git", "-C", str(ctx.product), "config", "core.hooksPath", str(hooks)], user=ctx.owner.name)
    exclude = ctx.checkout() / ".git" / "info" / "exclude"
    code, text = ctx.host.query(["cat", str(exclude)], user=AGENT_USER)
    current = text if code == 0 else ""
    if f"/{SCRATCH}/" not in current.splitlines():
        ctx.host.write(exclude, current + ("" if current.endswith("\n") or not current else "\n") + f"/{SCRATCH}/\n",
                       mode=0o644, owner=AGENT_USER)  # fmt: skip


def factory_repo(ctx: Install) -> str:
    """The factory's repo as the owner's checkout or root's record names it, never as the warden's settings do."""
    return github_name(ctx.factory_origin) or ""


def public_hosts(hosts: list[str]) -> list[str]:
    """The names of ``dashboard.hosts`` the box's edge serves over HTTPS: every one but loopback's."""
    return [name for name in hosts if name not in network.LOOPBACK]


def edge(ctx: Install) -> None:
    """Root writes the dashboard's site on the box's edge, so no agent can reroute the owner's dashboard. The edge
    fetches each name's certificate itself once the name's DNS points at the box. A site the edge refuses comes off
    again at once: left there, it would fail every reload of prod's deploys, and the edge's next start."""
    settings = ctx.need_settings()
    site = server_edge.site(public_hosts(settings.get_list("dashboard.hosts")), settings.get_int("dashboard.port"))
    _publish(ctx.host, layout.EDGE_SITE, site, "the dashboard's site", "dashboard.hosts")
    _staging(ctx)


def _staging(ctx: Install) -> None:
    """Slot 0, which is staging, at ``slots.zero_origin``: a site listening on the box's own address alone, with the
    product's blocked paths, and its port open in ufw, which the edge obeys on the host's network. The port the last
    site opened closes first, so neither a new port nor an empty setting leaves it open."""
    settings, host = ctx.need_settings(), ctx.host
    origin = settings.get_str("slots.zero_origin")
    opened = opened_port(host)
    if opened is not None:
        host.run(["ufw", "delete", *opening(opened)])
    if not origin:
        if opened is not None:
            host.remove(layout.SLOT0_SITE)
            host.run(server_edge.RELOAD)
        return
    address, _, port = origin.removeprefix("http://").partition(":")
    blocked = ctx.product_config().edge.blocked_paths
    site = server_edge.site([origin], settings.get_int("slots.port_base"), blocked, bind=address)
    _publish(host, layout.SLOT0_SITE, site, "staging's site", "slots.zero_origin")
    host.run(["ufw", *opening(port)])


def opened_port(host: Host) -> str | None:
    """The port staging's site on the edge serves, from its address; None when it has no site."""
    if not host.exists(layout.SLOT0_SITE):
        return None
    return server_edge.names(host.read(layout.SLOT0_SITE))[0].rpartition(":")[2]


def opening(port: str) -> list[str]:
    """The ufw rule that lets the internet reach ``port``."""
    return ["allow", f"{port}/tcp"]


def _publish(host: Host, path: Path, site: str, what: str, setting: str) -> None:
    host.write(path, site, mode=0o644)
    try:
        host.run(server_edge.RELOAD)
    except CommandError as exc:
        host.remove(path)
        host.run(server_edge.RELOAD)
        raise refuse(f"The edge refused {what}, which is off the edge again ({exc}): put {setting} right with --set, "
                     "then install again") from exc  # fmt: skip


def start(ctx: Install) -> None:
    host = ctx.host
    host.run(["systemctl", "daemon-reload"])
    host.run(["systemctl", "enable", layout.FIREWALL_UNIT, layout.FIREWALL_TIMER, *layout.SERVICES])
    host.run(["systemctl", "restart", layout.FIREWALL_UNIT, layout.FIREWALL_TIMER])
    host.run(["systemctl", "restart", *layout.SERVICES])


def install_digest(ctx: Install) -> str:
    """The install's hash, computed by the installed warden's own code, so the vitals compare like with like."""
    install = ctx.places.install
    code = (
        f"import sys; from pathlib import Path; sys.path.insert(0, {str(install)!r}); "
        f"from runtime.warden.health.vitals import install_digest; print(install_digest(Path({str(install)!r})))"
    )
    return ctx.host.run([layout.PYTHON, "-I", "-c", code]).strip() or "<digest>"


def record(ctx: Install) -> None:
    """Written last, after every step: until then the warden's vitals stay red and nothing starts."""
    state, host = ctx.places.state, ctx.host
    host.write(ctx.places.install_hash, install_digest(ctx) + "\n", mode=0o600, owner=WARDEN_USER)
    body = {"commit": ctx.sha, "subject": ctx.subject, "managed_settings": str(MANAGED_SETTINGS),
            "product": str(ctx.product)}  # fmt: skip
    host.write(layout.install_record(state), json.dumps(body, indent=1) + "\n", mode=0o600, owner=WARDEN_USER)
    # Root's own record, which root's upgrade unit trusts where it would not trust the warden's folder.
    host.mkdir(ctx.places.upgrade, mode=0o755)
    mine = {"commit": ctx.sha, "owner": ctx.owner.name, "product": str(ctx.product), "repo": factory_repo(ctx)}
    host.write(upgrades.installed_path(ctx.places), json.dumps(mine, indent=1) + "\n", mode=0o644)
    host.write(upgrades.settings_path(ctx.places), json.dumps(ctx.overrides, indent=1) + "\n", mode=0o600)
    host.say(f"the factory at {ctx.sha[:12]} is installed")


def dashboard_address(hosts: list[str]) -> str | None:
    """The dashboard as the edge serves it, by its first public name; None when it has none."""
    public = public_hosts(hosts)
    return f"https://{public[0]}/" if public else None
