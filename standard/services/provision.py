"""Each workspace's and slot's share of the host's shared services: a network, a database, error projects.

Every step is idempotent: a second run finds what the first made and changes nothing. Postgres and GlitchTip are
driven inside their own containers, so no superuser password leaves them. The values returned are the env keys the
factory writes into the workspace's env file at every deploy.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from standard.config_records import Config
from standard.envfile import parse_values
from standard.services import glitchtip_bootstrap as glitchtip

Run = Callable[[list[str], str | None], subprocess.CompletedProcess[str]]
TIMEOUT_SECONDS = 1800

POSTGRES = "factory-postgres"
LANGFUSE = "factory-langfuse"
GLITCHTIP = "factory-glitchtip"
SHARED = (POSTGRES, LANGFUSE, GLITCHTIP)
# The telemetry network reaches only traces and errors: what a product runs for others (its agents' containers) joins
# it instead of the services network, so it never has a route to the database.
TELEMETRY = (LANGFUSE, GLITCHTIP)
LANGFUSE_HOST = "http://factory-langfuse:3000"
GLITCHTIP_INGEST = "factory-glitchtip:8000"
SERVICE_KEYS = (
    "POSTGRES_PASSWORD",
    "LANGFUSE_INIT_PROJECT_PUBLIC_KEY",
    "LANGFUSE_INIT_PROJECT_SECRET_KEY",
    "GLITCHTIP_OWNER_PASSWORD",
)
# The keys the factory writes into an env file that name no secret: a network, an address, an environment and a
# release.
PUBLIC_KEYS = frozenset(
    {"FACTORY_SERVICES_NETWORK", "LANGFUSE_HOST", "LANGFUSE_TRACING_ENVIRONMENT", "SENTRY_ENVIRONMENT",
     "SENTRY_RELEASE", "SENTRY_INGEST_URL"}
)  # fmt: skip
# A value made only of digits or a boolean word carries no secret; hiding it would erase every port and flag.
_NOT_SECRET = re.compile(r"^(\d+|true|false|yes|no|on|off)$", re.IGNORECASE)
REDACTED = "[redacted]"
_SHORTEST_SECRET = 6
# Terse errors name no statement text, so a failed ALTER ROLE never prints the password it carried.
PSQL = [
    *("docker", "exec", "-i", POSTGRES, "psql", "-U", "postgres", "-d", "postgres", "-X", "-q"),
    *("-v", "ON_ERROR_STOP=1", "-v", "VERBOSITY=terse"),
]


def shell(container: str) -> list[str]:
    return ["docker", "exec", "-i", container, "./manage.py", "shell"]


class ProvisionError(Exception):
    """A shared service refused, or a command failed; the message says which and why."""


def secret_values(config: Config, values: Mapping[str, str]) -> list[str]:
    """Every value of an env file that could be a secret: none of factory.toml's [env.set] keys (made from the
    workspace's public facts: its name, port, address and folders), none of the factory's public keys, and no number
    or boolean word."""
    public = frozenset(key for key, _ in config.env.set) | PUBLIC_KEYS
    found = ((key, value.strip().strip("'\"")) for key, value in values.items())
    return [value for key, value in found if key not in public and value and not _NOT_SECRET.match(value)]


def redact(text: str, secrets: Iterable[str]) -> str:
    # Longest first, so a secret that contains another is replaced whole. A value of a few characters guards
    # nothing and would blank those letters out of every word, so it is left.
    for secret in sorted({s for s in secrets if len(s) >= _SHORTEST_SECRET}, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    return text


def real_run(argv: list[str], stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run without a shell; a timeout or a missing program comes back as a failed result, never an exception."""
    feed: dict[str, Any] = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=False, **feed)
    # telemetry: surfaced — a command that runs too long comes back as a failed `CompletedProcess`
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", f"timed out after {TIMEOUT_SECONDS} s")
    # telemetry: surfaced — a program that cannot start comes back as a failed `CompletedProcess`
    except OSError as exc:
        return subprocess.CompletedProcess(argv, 127, "", f"cannot start {argv[0]}: {exc}")


def checked(run: Run, argv: list[str], stdin: str | None = None) -> str:
    """The command's output; a non-zero exit raises naming the command and the end of what it printed."""
    done = run(argv, stdin)
    if done.returncode != 0:
        raise ProvisionError(f"`{' '.join(argv)}` failed (exit {done.returncode}): {tail(done.stderr or done.stdout)}")
    return done.stdout


def tail(text: str, lines: int = 5) -> str:
    found = [line.strip() for line in text.strip().splitlines() if line.strip()]
    return " / ".join(found[-lines:]) or "it printed nothing"


def read_services_env(path: Path) -> dict[str, str]:
    """The shared stack's generated secrets, as the set-up or the installer wrote them."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ProvisionError(
            f"{path}: cannot read the shared services' settings ({type(exc).__name__}: {exc})"
        ) from exc
    values, problems = parse_values(text, str(path))
    problems += [f"{path} has no {key}" for key in SERVICE_KEYS if not values.get(key)]
    if problems:
        raise ProvisionError("; ".join(problems))
    return values


def database(config: Config, workspace: str) -> str:
    """The name of both the workspace's database and its role."""
    return f"{config.product.name}_{workspace}".replace("-", "_")


def network(config: Config, workspace: str) -> str:
    return f"{config.project(workspace)}-services"


def telemetry_network(config: Config, workspace: str) -> str:
    return f"{config.project(workspace)}-telemetry"


def networks(run: Run) -> set[str]:
    return set(checked(run, ["docker", "network", "ls", "--format", "{{.Name}}"]).split())


def ensure_network(run: Run, name: str, present: set[str]) -> bool:
    """Create ``name`` unless ``present`` has it; True when it was created."""
    if name in present:
        return False
    checked(run, ["docker", "network", "create", name])
    return True


def bootstrap(config: Config, services: dict[str, str], run: Run) -> dict[str, str]:
    """The host's GlitchTip organization, owner, team and read-only token. No project of the factory's own lives
    here: the factory reports to its own GlitchTip on the laptop."""
    params = glitchtip.bootstrap_params(
        config.product.name, config.services.owner_email, services["GLITCHTIP_OWNER_PASSWORD"]
    )
    token = glitchtip_call(run, GLITCHTIP, params).get("token")
    if not isinstance(token, str) or not token:
        raise ProvisionError("GlitchTip's answer lacks the read-only token")
    return {"token": token}


def workspace(
    config: Config, services: dict[str, str], run: Run, *, workspace: str, origin: str, commit: str, state: Path
) -> dict[str, str]:
    """Provision one workspace or slot and return every env value the factory writes for it."""
    present = networks(run)
    net, telemetry_net = network(config, workspace), telemetry_network(config, workspace)
    for name, members in ((net, SHARED), (telemetry_net, TELEMETRY)):
        ensure_network(run, name, present)
        joined = _connected(run, name)
        for container in members:
            if container not in joined:
                checked(run, ["docker", "network", "connect", name, container])
    name = database(config, workspace)
    password = _password(state, name)
    checked(run, PSQL, _create_sql(name, password))
    slugs = {errors: f"{workspace}-{errors}" for errors in config.services.errors}
    dsns = glitchtip_call(run, GLITCHTIP, glitchtip.workspace_params(config.product.name, slugs)).get("dsns")
    if not isinstance(dsns, dict) or set(dsns) != set(slugs) or not all(isinstance(v, str) for v in dsns.values()):
        raise ProvisionError(f"GlitchTip's answer lacks a DSN for some of {', '.join(slugs)}")
    public = urlsplit(origin)
    keys = {errors: _dsn_parts(dsns[errors]) for errors in config.services.errors}
    return env_values(
        config, workspace=workspace, commit=commit, networks=(net, telemetry_net),
        database_url=f"postgresql://{name}:{password}@{POSTGRES}:5432/{name}", langfuse=(LANGFUSE_HOST,
        services["LANGFUSE_INIT_PROJECT_PUBLIC_KEY"], services["LANGFUSE_INIT_PROJECT_SECRET_KEY"]),
        ingest=f"http://{GLITCHTIP_INGEST}",
        dsn=lambda errors: f"http://{keys[errors][0]}@{GLITCHTIP_INGEST}/{keys[errors][1]}",
        public_dsn=lambda errors: f"{public.scheme}://{keys[errors][0]}@{public.netloc}"
        f"{config.services.errors_public_path}/{keys[errors][1]}",
    )  # fmt: skip


def env_values(
    config: Config,
    *,
    workspace: str,
    commit: str,
    networks: tuple[str, str],
    database_url: str,
    langfuse: tuple[str, str, str],
    ingest: str,
    dsn: Callable[[str], str],
    public_dsn: Callable[[str], str],
) -> dict[str, str]:
    """Every env value the factory writes for a workspace, the one list of their keys: ``langfuse`` is its host,
    public key and secret key, and ``dsn`` and ``public_dsn`` give each error project's addresses."""
    values = {
        "FACTORY_SERVICES_NETWORK": networks[0],
        "FACTORY_TELEMETRY_NETWORK": networks[1],
        "DATABASE_URL": database_url,
        "LANGFUSE_HOST": langfuse[0],
        "LANGFUSE_PUBLIC_KEY": langfuse[1],
        "LANGFUSE_SECRET_KEY": langfuse[2],
        "LANGFUSE_TRACING_ENVIRONMENT": workspace,
        "SENTRY_ENVIRONMENT": workspace,
        "SENTRY_RELEASE": commit,
        "SENTRY_INGEST_URL": ingest,
    }
    for errors in config.services.errors:
        suffix = errors.upper().replace("-", "_")
        values[f"SENTRY_DSN_{suffix}"] = dsn(errors)
        if config.services.errors_public_path:
            values[f"SENTRY_PUBLIC_DSN_{suffix}"] = public_dsn(errors)
    return values


def drop(config: Config, services: dict[str, str], run: Run, *, workspace: str, state: Path) -> None:
    """Remove the workspace's database, role and networks; its GlitchTip projects and their history stay."""
    name = database(config, workspace)
    checked(run, PSQL, f"DROP DATABASE IF EXISTS {_ident(name)} WITH (FORCE);\nDROP ROLE IF EXISTS {_ident(name)};\n")
    (state / f"{name}.password").unlink(missing_ok=True)
    present = networks(run)
    for net in (network(config, workspace), telemetry_network(config, workspace)):
        if net not in present:
            continue
        for container in sorted(_connected(run, net) & set(SHARED)):
            checked(run, ["docker", "network", "disconnect", net, container])
        checked(run, ["docker", "network", "rm", net])


def glitchtip_call(run: Run, container: str, params: dict[str, Any]) -> dict[str, Any]:
    """One action of the bootstrap script, run in the GlitchTip ``container``: its answer, or ProvisionError."""
    done = run(shell(container), glitchtip.script(params))
    found = glitchtip.answer(done.stdout)
    if isinstance(found, str):
        raise ProvisionError(f"GlitchTip ({container}) refused: {found} (exit {done.returncode}: {tail(done.stderr)})")
    if done.returncode != 0:
        raise ProvisionError(f"GlitchTip ({container}) failed after answering (exit {done.returncode})")
    return found


def _connected(run: Run, net: str) -> set[str]:
    raw = checked(run, ["docker", "network", "inspect", net, "--format", "{{json .Containers}}"])
    containers = json.loads(raw) if raw.strip() else None
    if not isinstance(containers, dict):
        return set()
    return {c["Name"] for c in containers.values() if isinstance(c, dict) and isinstance(c.get("Name"), str)}


def _password(state: Path, name: str) -> str:
    path = state / f"{name}.password"
    if path.exists():
        kept = path.read_text(encoding="utf-8").strip()
        if not kept or not kept.isalnum():
            raise ProvisionError(f"{path} holds no usable password; remove it to make a new one")
        return kept
    state.mkdir(parents=True, exist_ok=True)
    made = secrets.token_hex(24)
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(handle, "w", encoding="utf-8") as file:
        file.write(made + "\n")
    return made


def _create_sql(name: str, password: str) -> str:
    ident, literal = _ident(name), _literal(name)
    return (
        f"SELECT format('CREATE ROLE %I LOGIN', {literal})"
        f" WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = {literal})\\gexec\n"
        f"ALTER ROLE {ident} WITH LOGIN PASSWORD {_literal(password)};\n"
        f"SELECT format('CREATE DATABASE %I OWNER %I', {literal}, {literal})"
        f" WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = {literal})\\gexec\n"
        f"ALTER DATABASE {ident} OWNER TO {ident};\n"
        # Without this every role on the host could connect to every workspace's database.
        f"REVOKE ALL ON DATABASE {ident} FROM PUBLIC;\n"
    )


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _dsn_parts(dsn: str) -> tuple[str, str]:
    """A GlitchTip DSN's public key and project id."""
    parts = urlsplit(dsn)
    project = parts.path.rstrip("/").rsplit("/", 1)[-1]
    if not parts.username or not project.isdigit():
        raise ProvisionError(f"GlitchTip printed a DSN without a key and a project id: {dsn!r}")
    return parts.username, project
