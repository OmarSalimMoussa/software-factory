"""The shared services stack: its env file and bringing it up, on the product's server, where prod and the factory's
slots use it.

The stack is compose.yml beside this file, run as compose project factory-services from the server set-up's folder
(host_paths.FOLDER), which holds a copy of it and its services.env. The env file's secrets are made once and then
kept for good: the databases were created with them.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path

from standard import yaml_subset
from standard.config_records import Config
from standard.envfile import parse_values
from standard.services.provision import Run

COMPOSE = Path(__file__).parent / "compose.yml"
EDGE_CADDYFILE = Path(__file__).parent / "edge.Caddyfile"
PROJECT = "factory-services"
# A first start pulls every image and migrates every database before the stack answers healthy.
WAIT_SECONDS = 1200
ENV_FILE = "services.env"
# The loopback ports the stack publishes; the server serves the same ones to its tailnet.
PORTS = {"glitchtip": 8200, "pgweb": 8081, "postgres": 5434}
ONE_SHOT = ("glitchtip-migrate",)

_SECRET_BYTES: dict[str, int] = {
    "POSTGRES_PASSWORD": 32,
    "GLITCHTIP_SECRET_KEY": 32,
    "GLITCHTIP_POSTGRES_PASSWORD": 24,
    "GLITCHTIP_OWNER_PASSWORD": 16,
}

_HEADER = (
    "# The factory's shared services on this host (compose project factory-services).\n"
    "# Written by the factory: the secrets were made once and are kept; the rest follows factory.toml.\n"
)


class StackError(Exception):
    """The shared services did not come up; the message names each service that is not healthy."""


def public_url(config: Config, port: int) -> str:
    """Where a person opens a shared service: the server's tailnet name."""
    return f"https://{config.server.tailnet}:{port}"


def generate_env(config: Config, *, existing: dict[str, str]) -> dict[str, str]:
    """The shared services' settings: each secret ``existing`` holds kept, and one it lacks made. A secret it holds
    blank is refused: the data already stored was made with a value, which a new one would not open."""
    blank = sorted(key for key in _SECRET_BYTES if key in existing and not existing[key])
    if blank:
        raise StackError(f"{ENV_FILE} holds {', '.join(blank)} empty: put back the value the services were set up "
                         "with, or remove the services' data and the line to start them afresh")  # fmt: skip
    values = {key: existing.get(key) or secrets.token_hex(size) for key, size in _SECRET_BYTES.items()}
    values |= {
        "GLITCHTIP_DOMAIN": public_url(config, PORTS["glitchtip"]),
        "GLITCHTIP_FROM_EMAIL": config.services.owner_email,
        "ACME_EMAIL": config.server.acme_email,
    }
    return values


def render_env(values: dict[str, str]) -> str:
    return _HEADER + "".join(f"{key}={value}\n" for key, value in values.items())


def parse_env(text: str, where: str) -> dict[str, str]:
    values, problems = parse_values(text, where)
    if problems:
        raise StackError("; ".join(problems))
    return values


def services() -> list[str]:
    return list(yaml_subset.load(COMPOSE.read_text())["services"])


def compose_argv(folder: Path) -> list[str]:
    return ["docker", "compose", "-p", PROJECT, "--env-file", str(folder / ENV_FILE), "-f", str(folder / "compose.yml")]


def unhealthy(run: Run, folder: Path) -> list[str]:
    """Each service that is not up and healthy, with what it is doing instead; empty when the stack is well."""
    done = run([*compose_argv(folder), "ps", "--all", "--format", "json"], None)
    if done.returncode != 0:
        return [f"the stack cannot be read: {done.stderr.strip() or done.stdout.strip()}"]
    text = done.stdout.strip()
    rows = json.loads(text) if text.startswith("[") else [json.loads(line) for line in text.splitlines() if line]
    seen = {row["Service"]: row for row in rows}
    problems = []
    for name in services():
        row = seen.get(name)
        if row is None:
            problems.append(f"{name} (not created)")
            continue
        state, health = row["State"], row["Health"]
        if name in ONE_SHOT:
            if state != "exited" or row["ExitCode"] != 0:
                problems.append(f"{name} ({state}, exit code {row['ExitCode']})")
        elif state != "running" or health not in ("", "healthy"):
            problems.append(f"{name} ({state}{', ' + health if health else ''})")
    return problems


def up(run: Run, folder: Path) -> None:
    done = run([*compose_argv(folder), "up", "-d", "--wait", "--wait-timeout", str(WAIT_SECONDS)], None)
    if done.returncode == 0:
        return
    said = (done.stderr.strip() or done.stdout.strip()).splitlines()[-5:]
    bad = unhealthy(run, folder)
    named = ", ".join(bad) if bad else "compose did not say which service failed"
    raise StackError(f"the shared services did not come up healthy: {named}. Compose said: " + " | ".join(said))
