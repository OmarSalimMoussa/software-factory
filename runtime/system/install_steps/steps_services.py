"""The steps for the shared services the server set-up runs on this machine: check they run before anything changes,
let the stack user's runs read their settings, and make the factory's own GlitchTip project.

One copy of the services serves prod, every slot and the factory's own errors, on this machine's own Docker; the
install makes none of its own. The factory's errors go to a project of its own in the shared GlitchTip, made as each
slot's projects are, and its address is on the loopback, where the warden and the dashboard reach it.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from runtime.shared import error_reports
from runtime.shared.paths import DASHBOARD_USER, WARDEN_USER
from runtime.system import layout
from runtime.system.job import Install, refuse
from standard import config as product_config
from standard.services import provision, stack

SET_UP = "set the server up first (python3 -m standard.server.setup), then install again"


def check(ctx: Install) -> None:
    """The server set-up's shared services run here: their settings are in place, and every container a slot joins
    is running."""
    host, env = ctx.host, ctx.places.services / stack.ENV_FILE
    problems = [] if host.exists(env) else [f"{env} is missing"]
    _, out = host.query(["docker", "inspect", "--format", "{{.Name}} {{.State.Running}}", *provision.SHARED])
    running = {line.split()[0].lstrip("/") for line in out.splitlines() if line.strip().endswith(" true")}
    problems += [f"{name} is not running" for name in provision.SHARED if name not in running]
    if problems:
        raise refuse(f"The shared services every slot uses are not running on this computer ({'; '.join(problems)}): "
                     f"{SET_UP}")  # fmt: skip


def share(ctx: Install) -> None:
    """services.env, the server set-up's admin's alone, is readable by the stack user's group too: each slot's
    provisioning reads it."""
    env = ctx.places.services / stack.ENV_FILE
    ctx.host.run(["chgrp", layout.STACK_USER, str(env)])
    ctx.host.run(["chmod", "0640", str(env)])


def errors_project(ctx: Install) -> None:
    """The factory's own project in the shared GlitchTip, and its address on this machine's loopback, written for the
    warden and the dashboard, where their own errors go."""
    host = ctx.host
    try:
        config = product_config.load(ctx.product)
    except product_config.ConfigError as exc:
        raise refuse(f"The product's {product_config.FILE} cannot be used ({exc}): fix it in the product, then "
                     "install again") from exc  # fmt: skip
    name = error_reports.PROJECT
    if host.dry:
        host.say(f"would make the shared GlitchTip's {name} project in {config.product.name}'s organization")
        key, project = "<key>", "<project>"
    else:
        try:
            key, project = provision.projects(config, host.runner("", {}), {name: name})[name]
        except provision.ProvisionError as exc:
            host.say(f"GlitchTip could not be set up: {exc}")
            raise refuse(f"The shared GlitchTip gave the factory no project for its errors: {SET_UP}") from exc
    dsn = f"http://{key}@{urlsplit(layout.GLITCHTIP_URL).netloc}/{project}"
    host.write(error_reports.dsn_path(ctx.places), dsn + "\n", mode=0o640, owner=WARDEN_USER, group=DASHBOARD_USER)
