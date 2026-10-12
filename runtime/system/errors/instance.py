"""The factory's own GlitchTip, where the warden's and the dashboard's errors go.

It runs on the laptop as compose project factory-errors (compose.yml beside this file), from the errors folder's copy
of it and its errors.env, on the rootless Docker of its own user (layout.ERRORS_USER). No user that runs agent code
holds that Docker, reads errors.env or reaches its port, and no product's slot joins its network: the only key to it
is the factory-dsn secret the warden and the dashboard read. It runs the images the shared services' GlitchTip pins,
read from their compose file, so the two never drift apart. errors.env's secrets are made once and kept, as the
shared services' are. The install makes its organization, owner and project with the script the shared GlitchTip
takes, and saves no address but this one's own.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from urllib.parse import urlsplit

from standard import yaml_subset
from standard.services import glitchtip_bootstrap as glitchtip
from standard.services import provision, stack
from standard.services.provision import Run

PROJECT = "factory-errors"
COMPOSE = Path(__file__).parent / "compose.yml"
ENV_FILE = "errors.env"
PORT = 8210
URL = f"http://127.0.0.1:{PORT}"
HEALTH = f"{URL}/_health/"
CONTAINER = "factory-errors-glitchtip"
ORG = "factory"
_SECRETS = {"ERRORS_SECRET_KEY": 32, "ERRORS_POSTGRES_PASSWORD": 24, "ERRORS_OWNER_PASSWORD": 16}
# Each image it runs, by the shared services' service whose image it is.
_IMAGES = {
    "ERRORS_GLITCHTIP_IMAGE": "glitchtip",
    "ERRORS_POSTGRES_IMAGE": "glitchtip-postgres",
    "ERRORS_VALKEY_IMAGE": "glitchtip-valkey",
}
_HEADER = (
    "# The factory's own GlitchTip on this laptop (compose project factory-errors).\n"
    "# Written by the factory: the secrets were made once and are kept.\n"
)


class ErrorsError(Exception):
    """The factory's own GlitchTip cannot be set up; the message says why and what to do."""


def images() -> dict[str, str]:
    """The images the shared services' GlitchTip runs, as their compose file pins them."""
    try:
        services = yaml_subset.load(stack.COMPOSE.read_text(encoding="utf-8"))["services"]
        return {key: str(services[name]["image"]) for key, name in _IMAGES.items()}
    except (OSError, yaml_subset.YamlError, KeyError, TypeError) as exc:
        raise ErrorsError(f"{stack.COMPOSE} names no image for the shared GlitchTip's {', '.join(_IMAGES.values())} "
                          f"({type(exc).__name__}: {exc})") from exc  # fmt: skip


def generate_env(owner_email: str, existing: dict[str, str]) -> dict[str, str]:
    """errors.env's values: each secret ``existing`` holds kept, one it lacks made, and one held blank refused, since
    the database was made with a value a new one would not open."""
    blank = sorted(key for key in _SECRETS if key in existing and not existing[key])
    if blank:
        raise ErrorsError(f"{ENV_FILE} holds {', '.join(blank)} empty: put back the value the factory's GlitchTip was "
                          "set up with, or remove its data and the line to start it afresh")  # fmt: skip
    values = {key: existing.get(key) or secrets.token_hex(size) for key, size in _SECRETS.items()}
    return values | {"ERRORS_DOMAIN": URL, "ERRORS_FROM_EMAIL": owner_email, "ERRORS_PORT": str(PORT), **images()}


def render_env(values: dict[str, str]) -> str:
    return _HEADER + "".join(f"{key}={value}\n" for key, value in values.items())


def compose_argv(folder: Path) -> list[str]:
    return ["docker", "compose", "-p", PROJECT, "--env-file", str(folder / ENV_FILE), "-f", str(folder / "compose.yml")]


def up(run: Run, folder: Path) -> None:
    done = run([*compose_argv(folder), "up", "-d", "--wait", "--wait-timeout", str(stack.WAIT_SECONDS)], None)
    if done.returncode != 0:
        said = (done.stderr.strip() or done.stdout.strip()).splitlines()[-5:]
        raise ErrorsError("the factory's own GlitchTip did not come up healthy. Compose said: " + " | ".join(said))


def dsn(run: Run, owner_email: str, owner_password: str) -> str:
    """The factory project's address, with its organization and the owner made first (each found when made before)."""
    provision.glitchtip_call(run, CONTAINER, glitchtip.bootstrap_params(ORG, owner_email, owner_password))
    found = provision.glitchtip_call(run, CONTAINER, glitchtip.workspace_params(ORG, {ORG: ORG})).get("dsns")
    if not isinstance(found, dict) or not isinstance(found.get(ORG), str) or not found[ORG]:
        raise ErrorsError(f"the factory's own GlitchTip gave no address for its {ORG!r} project")
    return str(found[ORG])


def is_own(address: str) -> bool:
    """Whether ``address`` is this GlitchTip's: on the loopback, at its own port. Any other, a product's GlitchTip
    above all, is never saved as the factory's."""
    parts = urlsplit(address)
    return (parts.hostname, parts.port) == ("127.0.0.1", PORT)
