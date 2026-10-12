"""The steps for the laptop's shared services and the factory's own GlitchTip: their files, starting each as its own
user, the shared GlitchTip's read token and Langfuse's keys for the warden, and the factory's own GlitchTip's address
for the warden and the dashboard.

The stack user's rootless Docker runs the shared services, and the errors user's runs the factory's own GlitchTip:
each stack's files are root's, and only its own user reads them. The secrets in services.env and errors.env are made
once and kept at every reinstall: the databases were created with them. The images, the largest download of a first
install, are pulled in the background from as soon as Docker runs.
"""

from __future__ import annotations

import json
from urllib.parse import urlsplit

from runtime.shared import error_reports
from runtime.shared.paths import DASHBOARD_USER, WARDEN_USER
from runtime.system import layout
from runtime.system.errors import instance
from runtime.system.host import CommandError
from runtime.system.job import Install, refuse
from standard import config as product_config
from standard.config_records import Config
from standard.services import provision, stack

# What a server's edge reads; the laptop runs no edge.
_SERVER_ONLY = frozenset({stack.EDGE_CADDYFILE.name})
_IMAGES = "the shared services' images"
_ERRORS_IMAGES = "the factory's own GlitchTip's images"


def _config(ctx: Install) -> Config:
    try:
        return product_config.load(ctx.product)
    except product_config.ConfigError as exc:
        raise refuse(
            f"The product's {product_config.FILE} cannot be used ({exc}): fix it in the product, then install again"
        ) from exc


def _docker_env(ctx: Install, user: str = layout.STACK_USER) -> dict[str, str]:
    return {"DOCKER_HOST": f"unix://{layout.rootless_socket(ctx.uid(user))}"}


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
        raise refuse(f"The shared services' settings in {env} cannot be read ({exc}): fix the file by hand, keeping "
                     "its secrets, then install again") from exc  # fmt: skip
    try:
        values = stack.generate_env(config, kind="laptop", existing=existing)
    except stack.StackError as exc:
        raise refuse(f"The shared services' settings in {env} cannot be kept: {exc}. Then install again") from exc
    host.write(env, stack.render_env(values), mode=0o640, group=group)
    host.say("kept the secrets services.env held" if existing else "made services.env's secrets")
    _place_errors(ctx, config)


def _place_errors(ctx: Install, config: Config) -> None:
    host, folder, group = ctx.host, ctx.places.errors, layout.ERRORS_USER
    host.mkdir(folder, mode=0o750, group=group)
    compose = folder / "compose.yml"
    host.run(["cp", str(ctx.tree() / "runtime" / "system" / "errors" / "compose.yml"), str(compose)])
    host.run(["chown", f"root:{group}", str(compose)])
    host.run(["chmod", "0640", str(compose)])
    env = folder / instance.ENV_FILE
    try:
        existing = stack.parse_env(host.read(env), str(env)) if host.exists(env) else {}
        values = instance.generate_env(config.services.owner_email, existing)
    except (stack.StackError, instance.ErrorsError) as exc:
        raise refuse(f"The factory's own GlitchTip's settings in {env} cannot be kept ({exc}): fix the file by hand, "
                     "keeping its secrets, then install again") from exc  # fmt: skip
    host.write(env, instance.render_env(values), mode=0o640, group=group)
    host.say("kept the secrets errors.env held" if existing else "made errors.env's secrets")


def _stacks(ctx: Install) -> tuple[tuple[str, str, list[str], str], ...]:
    """Each stack the install starts: what its download is called, its user, its compose command and its unit."""
    return (
        (_IMAGES, layout.STACK_USER, stack.compose_argv(ctx.places.services, edge=False), layout.SERVICES_UNIT),
        (_ERRORS_IMAGES, layout.ERRORS_USER, instance.compose_argv(ctx.places.errors), layout.ERRORS_UNIT),
    )


def pull(ctx: Install) -> None:
    """Start downloading every image each stack runs, as its own user, and go on: the start waits."""
    for name, user, compose, _ in _stacks(ctx):
        ctx.pending[name] = ctx.host.background([*compose, "pull", "--quiet"], user=user, env=_docker_env(ctx, user))


def start(ctx: Install) -> None:
    """Once its images are here, each stack is enabled, so it comes back after a reboot. Already running, it reloads:
    a changed service is recreated and the rest keep running, with each slot's data. A download that failed in the
    background (a network blip, a daemon still starting) is tried once more, here."""
    for name, user, compose, unit in _stacks(ctx):
        code, _ = ctx.await_pending(name)
        if code != 0:
            try:
                ctx.host.run([*compose, "pull", "--quiet"], user=user, env=_docker_env(ctx, user))
            except CommandError as exc:
                raise refuse(f"{name[0].upper()}{name[1:]} could not be downloaded: check this computer's internet "
                             "connection, then install again") from exc  # fmt: skip
        ctx.host.run(["systemctl", "enable", unit])
        ctx.host.run(["systemctl", "reload-or-restart", unit])


def keys(ctx: Install) -> None:
    """The shared GlitchTip's organization, the owner's account and a read-only token, made as the stack user, and
    the factory's own GlitchTip's organization, owner and project, made as the errors user (each step finds what an
    earlier install made). The token and Langfuse's project keys are the warden's, for reading a slot's errors and
    traces; the factory project's DSN is the warden's and the dashboard's, where their own errors go, and is never any
    GlitchTip but the factory's own."""
    host, config = ctx.host, _config(ctx)
    public_key, secret_key = "LANGFUSE_INIT_PROJECT_PUBLIC_KEY", "LANGFUSE_INIT_PROJECT_SECRET_KEY"
    if host.dry:
        host.say(f"would make the shared GlitchTip's {config.product.name} organization and a read token as "
                 f"{layout.STACK_USER}, and the factory's own GlitchTip's project as {layout.ERRORS_USER}")  # fmt: skip
        found, factory_dsn = {"token": "<token>"}, "<dsn>"
        services = {public_key: "<public key>", secret_key: "<secret key>"}
    else:
        run = host.runner(layout.STACK_USER, _docker_env(ctx))
        errors_env = ctx.places.errors / instance.ENV_FILE
        try:
            services = provision.read_services_env(ctx.places.services / stack.ENV_FILE)
            found = provision.bootstrap(config, services, run)
            password = stack.parse_env(host.read(errors_env), str(errors_env))["ERRORS_OWNER_PASSWORD"]
            own = host.runner(layout.ERRORS_USER, _docker_env(ctx, layout.ERRORS_USER))
            factory_dsn = instance.dsn(own, config.services.owner_email, password)
            error_reports.parse_dsn(factory_dsn)
        except (provision.ProvisionError, instance.ErrorsError, stack.StackError, KeyError,
                error_reports.DsnError) as exc:  # fmt: skip
            host.say(f"GlitchTip could not be set up: {exc}")
            raise refuse("GlitchTip, the shared one or the factory's own, could not be set up: install again; if it "
                         "stops here again, the log says why") from exc  # fmt: skip
        if not instance.is_own(factory_dsn):
            given = urlsplit(factory_dsn).netloc.rpartition("@")[2]
            raise refuse(f"The factory's own GlitchTip gave {given} as its address, not its own "
                         f"{instance.URL}, so the factory saves no address for its errors: restart "
                         f"{layout.ERRORS_UNIT}, which reads {errors_env} again, then install again")  # fmt: skip
    folder = layout.secrets(ctx.places.state)
    keys = {
        layout.LAPTOP_GLITCHTIP: {"url": layout.GLITCHTIP_URL, "org": config.product.name, "token": found["token"]},
        layout.LAPTOP_LANGFUSE: {"url": layout.LANGFUSE_URL, "public_key": services[public_key],
                                 "secret_key": services[secret_key]},
    }  # fmt: skip
    for name, body in keys.items():
        host.write(folder / name, json.dumps(body, indent=1) + "\n", mode=0o600, owner=WARDEN_USER)
    host.write(error_reports.dsn_path(ctx.places), factory_dsn + "\n", mode=0o640, owner=WARDEN_USER,
               group=DASHBOARD_USER)  # fmt: skip
