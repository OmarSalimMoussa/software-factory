"""The steps that wire the factory in: managed settings, units, polkit, the firewall, the factory command, the git
hooks, the tailnet, starting it, and the record a finished install leaves."""

from __future__ import annotations

import json
import re
from pathlib import Path

from runtime.shared.paths import AGENT_USER, MANAGED_SETTINGS, SCRATCH, SLICE, WARDEN_USER
from runtime.system import layout, render
from runtime.system.host import CommandError, Host
from runtime.system.job import Install, refuse

_HTTPS = 443
_TAILSCALE_LINK = re.compile(r"https://login\.tailscale\.com/\S+")


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
    return render.values(ctx.places, ctx.need_settings(), agent_uid=ctx.uid(AGENT_USER),
                         stack_uid=ctx.uid(layout.STACK_USER))  # fmt: skip


def units(ctx: Install) -> None:
    values = given(ctx)
    for item in render.UNIT_FILES:
        ctx.host.write(item.dest, render.artifact(item, ctx.templates(), values), mode=item.mode)
    # The warden's run-time budget changes live here; the slice just written already holds the settled values.
    control = layout.SLICE_CONTROL / f"{SLICE}.d"
    if ctx.host.exists(control):
        ctx.host.remove(control)
    ctx.host.run(["systemctl", "daemon-reload"])


def polkit(ctx: Install) -> None:
    text = render.artifact(render.POLKIT, ctx.templates(), given(ctx))
    ctx.host.write(render.POLKIT.dest, text, mode=render.POLKIT.mode)


def firewall(ctx: Install) -> None:
    ctx.host.mkdir(layout.CONFIG, mode=0o755)
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


def https_handlers(status: str) -> dict[str, str] | None:
    """What the tailnet serves on its HTTPS port, each path to its target, from `tailscale serve status --json`; None
    when the answer cannot be read."""
    try:
        data = json.loads(status or "{}")
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    found: dict[str, str] = {}
    for where, web in (data.get("Web") or {}).items():
        if not isinstance(web, dict) or where.rpartition(":")[2] != str(_HTTPS):
            continue
        for path, handler in (web.get("Handlers") or {}).items():
            target = (
                handler.get("Proxy") or handler.get("Path") or handler.get("Text") if isinstance(handler, dict) else ""
            )
            found[path] = str(target or "a handler")
    forward = ((data.get("TCP") or {}).get(str(_HTTPS)) or {}).get("TCPForward")
    if forward:
        found["(every connection)"] = str(forward)
    return found


def read_tailnet_record(host: Host, state: Path) -> dict[str, object]:
    """What an earlier install changed on Tailscale (``layout.tailnet_record``); empty when it changed nothing yet."""
    path = layout.tailnet_record(state)
    if not host.exists(path):
        return {}
    try:
        found = json.loads(host.read(path))
    except ValueError as exc:
        raise refuse(f"{path} cannot be read ({exc}): remove it, then try again") from exc
    return found if isinstance(found, dict) else {}


def _operator(prefs: str) -> str | None:
    """The Tailscale operator `tailscale debug prefs` names: "" for none, None when its answer cannot be read."""
    try:
        found = json.loads(prefs).get("OperatorUser", "")
    except (ValueError, AttributeError):
        return None
    return found if isinstance(found, str) else None


def tailnet(ctx: Install) -> None:
    """Only root may change what the tailnet serves, so no agent can reroute the owner's dashboard. On a tailnet whose
    HTTPS is off, tailscale waits for the owner to turn it on at a link it prints: past the wait, the owner is shown
    that link. The dashboard never takes over a port 443 that serves something else, and the operator the install
    clears is kept for the uninstall to put back."""
    settings, host = ctx.need_settings(), ctx.host
    port = settings.get_int("dashboard.port")
    ours = f"http://127.0.0.1:{port}"
    record = read_tailnet_record(host, ctx.places.state)
    code, status = host.query(["tailscale", "serve", "status", "--json"])
    handlers = https_handlers(status) if code == 0 else None
    if handlers is None and not host.dry:
        raise refuse("Tailscale did not say what your tailnet serves (tailscale serve status --json): check that "
                     "tailscale runs, then install again")  # fmt: skip
    others = sorted(f"{target} at {path}" for path, target in (handlers or {}).items()
                    if target not in (ours, record.get("served")))  # fmt: skip
    if others:
        raise refuse(f"Your tailnet already serves {', '.join(others)} on port {_HTTPS}, which the dashboard takes: "
                     "move it to another port, then install again")  # fmt: skip
    if "operator" not in record:
        code, prefs = host.query(["tailscale", "debug", "prefs"])
        record["operator"] = _operator(prefs) if code == 0 else None
    record["served"] = ours
    host.write(layout.tailnet_record(ctx.places.state), json.dumps(record) + "\n", mode=0o600)
    host.run(["tailscale", "set", "--operator="])
    serve = ["tailscale", "serve", "--yes", "--bg", f"--https={_HTTPS}", f"http://127.0.0.1:{port}"]
    try:
        ctx.host.run(serve, timeout=settings.get_int("install.tailnet_seconds"))
    except CommandError as exc:
        link = _TAILSCALE_LINK.search(exc.output)
        if link is None:
            raise
        raise refuse(
            f"Turn on HTTPS for your tailnet, so the dashboard gets its address: open {link.group(0)}, "
            "approve it, then install again"
        ) from exc


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


def record(ctx: Install, drills: list[dict[str, object]]) -> None:
    """Written last, after every drill passed: until then the warden's vitals stay red and nothing starts."""
    state, host = ctx.places.state, ctx.host
    host.write(ctx.places.install_hash, install_digest(ctx) + "\n", mode=0o600, owner=WARDEN_USER)
    # The owner's own checkout of the product too: the install console offers it for the next install.
    body = {"commit": ctx.sha, "subject": ctx.subject, "drills": drills, "managed_settings": str(MANAGED_SETTINGS),
            "product": str(ctx.product)}  # fmt: skip
    host.write(layout.install_record(state), json.dumps(body, indent=1) + "\n", mode=0o600, owner=WARDEN_USER)
    host.say(f"the factory at {ctx.sha[:12]} is installed")


def dashboard_address(host: Host) -> str:
    """The dashboard on the tailnet, where `tailscale serve` publishes it; "" when tailscale names no machine."""
    code, out = host.query(["tailscale", "status", "--json"])
    try:
        name = json.loads(out)["Self"]["DNSName"] if code == 0 else ""
    # telemetry: surfaced — the owner is shown no address
    except (ValueError, KeyError, TypeError):
        return ""
    return f"https://{name.strip('.')}/" if isinstance(name, str) and name.strip(".") else ""
