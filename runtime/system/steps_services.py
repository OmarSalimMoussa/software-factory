"""The steps for the laptop's shared services: their files, starting them as the stack user, and the GlitchTip keys
the warden and the dashboard keep from them.

The stack user's rootless Docker runs them, so the files are root's and the stack user only reads them. The secrets
in services.env are made once and kept at every reinstall: the databases were created with them.
"""

from __future__ import annotations

import json

from runtime.shared import error_reports
from runtime.shared.paths import DASHBOARD_USER, WARDEN_USER
from runtime.system import layout
from runtime.system.job import Install, refuse
from standard import config as product_config
from standard.services import provision, stack

# What a server's edge reads; the laptop runs no edge.
_SERVER_ONLY = frozenset({stack.EDGE_CADDYFILE.name})


def _config(ctx: Install) -> product_config.Config:
    try:
        return product_config.load(ctx.product)
    except product_config.ConfigError as exc:
        raise refuse(f"the product's {product_config.FILE} cannot be used: {exc}") from exc


def place(ctx: Install) -> None:
    """The stack's files from the sealed commit, copied onto what is there so a bound file keeps its inode; then
    services.env, its secrets kept and the rest made again from the product's factory.toml."""
    host, folder, group = ctx.host, ctx.places.services, layout.STACK_USER
    config = _config(ctx)
    host.mkdir(folder, mode=0o750, group=group)
    source = ctx.tree() / "standard" / "services"
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if path.is_dir() or path.suffix == ".py" or "__pycache__" in relative.parts or path.name in _SERVER_ONLY:
            continue
        dest = folder / relative
        if dest.parent != folder:
            host.mkdir(dest.parent, mode=0o750, group=group)
        host.run(["cp", str(path), str(dest)])
        host.run(["chown", f"root:{group}", str(dest)])
        host.run(["chmod", "0640", str(dest)])
    env = folder / stack.ENV_FILE
    try:
        existing = stack.parse_env(host.read(env), str(env)) if host.exists(env) else {}
    except ValueError as exc:
        raise refuse(f"{exc}: fix it by hand, keeping its secrets, then install again") from exc
    values = stack.generate_env(config, kind="laptop", existing=existing)
    host.write(env, stack.render_env(values), mode=0o640, group=group)
    host.say("  kept the secrets services.env held" if existing else "  made services.env's secrets")


def start(ctx: Install) -> None:
    """Enabled, so they come back after a reboot. Already running, they reload: a changed service is recreated and
    the rest keep running, with each slot's data."""
    ctx.host.run(["systemctl", "enable", layout.SERVICES_UNIT])
    ctx.host.run(["systemctl", "reload-or-restart", layout.SERVICES_UNIT])


def keys(ctx: Install) -> None:
    """GlitchTip's organization, the owner's account, a read-only token and the factory's own project, made as the
    stack user (each step finds what an earlier install made). The token and Langfuse's project keys are the
    warden's, for reading a slot's errors and traces; the project's DSN is the warden's and the dashboard's, where
    their own errors go."""
    host, config = ctx.host, _config(ctx)
    public_key, secret_key = "LANGFUSE_INIT_PROJECT_PUBLIC_KEY", "LANGFUSE_INIT_PROJECT_SECRET_KEY"
    if host.dry:
        host.say(f"  would make GlitchTip's {config.product.name} organization, a read token and the factory's own "
                 f"project, as {layout.STACK_USER}")  # fmt: skip
        found = {"token": "<token>", "factory_dsn": "<dsn>"}
        services = {public_key: "<public key>", secret_key: "<secret key>"}
    else:
        socket = layout.rootless_socket(ctx.uid(layout.STACK_USER))
        run = host.runner(layout.STACK_USER, {"DOCKER_HOST": f"unix://{socket}"})
        try:
            services = provision.read_services_env(ctx.places.services / stack.ENV_FILE)
            found = provision.bootstrap(config, services, run)
            error_reports.parse_dsn(found["factory_dsn"])
        except (provision.ProvisionError, error_reports.DsnError) as exc:
            raise refuse(f"GlitchTip could not be set up for the factory: {exc}") from exc
    folder = layout.secrets(ctx.places.state)
    keys = {
        layout.LAPTOP_GLITCHTIP: {"url": layout.GLITCHTIP_URL, "org": config.product.name, "token": found["token"]},
        layout.LAPTOP_LANGFUSE: {"url": layout.LANGFUSE_URL, "public_key": services[public_key],
                                 "secret_key": services[secret_key]},
    }  # fmt: skip
    for name, body in keys.items():
        host.write(folder / name, json.dumps(body, indent=1) + "\n", mode=0o600, owner=WARDEN_USER)
    host.write(error_reports.dsn_path(ctx.places), found["factory_dsn"] + "\n", mode=0o640, owner=WARDEN_USER,
               group=DASHBOARD_USER)  # fmt: skip
