"""The steps that wire the factory in: managed settings, units, polkit, the firewall, the factory command, the git
hooks, the tailnet, starting it, and the record a finished install leaves."""

from __future__ import annotations

import json
from pathlib import Path

from runtime.shared.paths import AGENT_USER, MANAGED_SETTINGS, SCRATCH, SLICE, WARDEN_USER
from runtime.system import layout, render
from runtime.system.job import Install, refuse

_HTTPS = 443


def managed_settings(ctx: Install) -> None:
    text = render.managed_settings(ctx.places, ctx.need_settings(), ctx.templates())
    host = ctx.host
    if ctx.first and host.exists(MANAGED_SETTINGS) and host.read(MANAGED_SETTINGS) != text:
        raise refuse(f"{MANAGED_SETTINGS} holds someone else's settings: move it aside, then install again")
    host.mkdir(MANAGED_SETTINGS.parent, mode=0o755)
    host.write(MANAGED_SETTINGS, text, mode=render.MANAGED.mode)


def _given(ctx: Install) -> dict[str, str]:
    return render.values(ctx.places, ctx.need_settings(), agent_uid=ctx.uid(AGENT_USER),
                         stack_uid=ctx.uid(layout.STACK_USER))  # fmt: skip


def units(ctx: Install) -> None:
    given = _given(ctx)
    for item in render.UNIT_FILES:
        ctx.host.write(item.dest, render.artifact(item, ctx.templates(), given), mode=item.mode)
    # The warden's run-time budget changes live here; the slice just written already holds the settled values.
    control = layout.SLICE_CONTROL / f"{SLICE}.d"
    if ctx.host.exists(control):
        ctx.host.remove(control)
    ctx.host.run(["systemctl", "daemon-reload"])


def polkit(ctx: Install) -> None:
    given = _given(ctx)
    ctx.host.write(render.POLKIT.dest, render.artifact(render.POLKIT, ctx.templates(), given), mode=render.POLKIT.mode)


def firewall(ctx: Install) -> None:
    given = _given(ctx)
    ctx.host.mkdir(layout.CONFIG, mode=0o755)
    text = render.artifact(render.FIREWALL, ctx.templates(), given)
    ctx.host.write(render.FIREWALL.dest, text, mode=render.FIREWALL.mode)
    code, out = ctx.host.query(["nft", "-c", "-f", str(render.FIREWALL.dest)])
    if code != 0:
        raise refuse(f"nft refuses the factory's firewall: {out.strip()[-300:]}")


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


def tailnet(ctx: Install) -> None:
    """Only root may change what the tailnet serves, so no agent can reroute the owner's dashboard."""
    port = ctx.need_settings().get_int("dashboard.port")
    ctx.host.run(["tailscale", "set", "--operator="])
    ctx.host.run(["tailscale", "serve", "--bg", f"--https={_HTTPS}", f"http://127.0.0.1:{port}"])


def start(ctx: Install) -> None:
    host = ctx.host
    host.run(["systemctl", "daemon-reload"])
    host.run(["systemctl", "enable", layout.FIREWALL_UNIT, *layout.SERVICES])
    host.run(["systemctl", "restart", layout.FIREWALL_UNIT])
    host.run(["systemctl", "restart", *layout.SERVICES])


def install_digest(ctx: Install) -> str:
    """The install's hash, computed by the installed warden's own code, so the vitals compare like with like."""
    install = ctx.places.install
    code = (
        f"import sys; from pathlib import Path; sys.path.insert(0, {str(install)!r}); "
        f"from runtime.warden.vitals import install_digest; print(install_digest(Path({str(install)!r})))"
    )
    return ctx.host.run([layout.PYTHON, "-I", "-c", code]).strip() or "<digest>"


def record(ctx: Install, drills: list[dict[str, object]]) -> None:
    """Written last, after every drill passed: until then the warden's vitals stay red and nothing starts."""
    state, host = ctx.places.state, ctx.host
    host.write(ctx.places.install_hash, install_digest(ctx) + "\n", mode=0o600, owner=WARDEN_USER)
    body = {"commit": ctx.sha, "drills": drills, "managed_settings": str(MANAGED_SETTINGS)}
    host.write(layout.install_record(state), json.dumps(body, indent=1) + "\n", mode=0o600, owner=WARDEN_USER)
    if ctx.stage is not None and not host.dry:
        host.remove(Path(ctx.stage))
    host.say(f"The factory at {ctx.sha[:12]} is installed.")
