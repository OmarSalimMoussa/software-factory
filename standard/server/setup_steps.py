"""The set-up's steps, each printing what it found on the server (or this laptop) and what it changed: the admin user,
the product's settings, the folders, the shared services, the weekly clean-up, prod's heartbeat, the tailnet and the
warden's copy on this laptop. A Job carries what every step reads.
"""

from __future__ import annotations

import json
import os
import shlex
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from standard import envfile
from standard.config_records import Config
from standard.server import edge
from standard.server import heartbeat as heartbeat_mod
from standard.server.setup_machine import Host, SetupError, ensure_dirs, ensure_file, remote_text
from standard.services import host_paths, provision, stack
from standard.services.provision import Run

CODE = "/opt/software-factory"


PRODUCT_TOML = str(heartbeat_mod.PRODUCT_TOML)


CRON = "/etc/cron.d/factory-gc"


HEARTBEAT_CRON = "/etc/cron.d/factory-heartbeat"


COMMANDS = {
    "factory-gc": "standard.server.gc",
    "factory-heartbeat": "standard.server.heartbeat",
}


SERVE_HTTPS = tuple(stack.PORTS[name] for name in ("glitchtip", "pgweb"))


SERVE_TCP = (stack.PORTS["postgres"],)


# -I keeps the caller's environment and current folder off the import path, so the path is set here instead.
WRAPPER = """#!/usr/bin/python3 -I
import runpy
import sys

sys.path.insert(0, "{code}")
runpy.run_module("{module}", run_name="__main__", alter_sys=True)
"""


@dataclass
class Job:
    config: Config
    product_toml: str
    host: Host
    local: Run
    fresh: bool
    home: Path  # the owner's ~/.config/software-factory
    factory: Path
    admin_group: str = ""
    services: dict[str, str] = field(default_factory=dict)
    token: str = ""
    todo: list[str] = field(default_factory=list)
    ping: str = ""  # prod's Healthchecks.io ping address; "" keeps the one the server holds

    @property
    def admin(self) -> str:
        return f"{self.config.server.admin}:{self.admin_group}"


def on_laptop(job: Job, argv: list[str], what: str) -> str:
    done = job.local(argv, None)
    if done.returncode != 0:
        raise SetupError([f"{what}: {(done.stderr or done.stdout).strip()}"])
    return done.stdout


def users(job: Job) -> None:
    host, admin = job.host, job.config.server.admin
    job.admin_group = host.must(["id", "-gn", admin], "reading the admin user's group").strip()
    if "docker" in host.must(["id", "-nG", admin], "reading the admin user's groups").split():
        print(f"   found: {admin} is in the docker group")
    else:
        host.change(f"add {admin} to the docker group", ["sudo", "-n", "usermod", "-aG", "docker", admin])


def product_settings(job: Job) -> None:
    """The server's factory.toml, the admin's: prod's deploy runs as the admin and refreshes it at each push."""
    ensure_dirs(job.host, [(str(Path(PRODUCT_TOML).parent), "root:root", "755")])
    ensure_file(job.host, PRODUCT_TOML, job.product_toml, owner=job.admin, mode="644")


def _wrapper(name: str) -> str:
    return WRAPPER.format(code=CODE, module=COMMANDS[name])


def folders(job: Job) -> None:
    owner, name = job.admin, job.config.product.name
    wanted = [(path, owner, "755") for path in ("/srv/factory", str(host_paths.FOLDER), "/srv/factory/edge")]
    wanted += [(path, owner, "755") for path in (str(edge.SITES), "/srv/factory/state", f"/srv/{name}")]
    for ws in job.config.workspaces:
        base = f"/srv/{name}/{ws.name}"
        wanted += [(base, owner, "755"), (f"{base}/stack", owner, "755"), (f"{base}/env", owner, "700")]
        wanted.append((f"{base}/state", owner, "755"))
    ensure_dirs(job.host, wanted)


def shared_services(job: Job) -> None:
    host, owner = job.host, job.admin
    changed = ensure_file(host, f"{host_paths.FOLDER}/compose.yml", stack.COMPOSE.read_text(), owner=owner, mode="644")
    # The edge holds this file through a bind mount, which keeps the inode it started with: write it in place.
    caddyfile = stack.EDGE_CADDYFILE.read_text()
    edge_changed = ensure_file(
        host, f"{host_paths.FOLDER}/edge.Caddyfile", caddyfile, owner=owner, mode="644", in_place=True
    )
    env_path = f"{host_paths.FOLDER}/{stack.ENV_FILE}"
    found = remote_text(host, env_path)
    existing = stack.parse_env(found[1], env_path) if found else {}
    job.services = stack.generate_env(job.config, existing=existing)
    changed |= ensure_file(host, env_path, stack.render_env(job.services), owner=owner, mode="600")
    bad = stack.unhealthy(host.run, host_paths.FOLDER)
    if bad:
        print("   found: not up and healthy: " + ", ".join(bad))
    if bad or changed:
        host.act("bring the shared services up and wait until healthy", lambda: stack.up(host.run, host_paths.FOLDER))
    else:
        print("   found: every shared service up and healthy")
    if edge_changed:
        host.change("reload the edge's settings", edge.RELOAD)
    _reconnect(job)
    if host.dry_run:
        print("   would make sure of GlitchTip's organization, owner and read-only token")
        return
    job.token = provision.bootstrap(job.config, job.services, host.run)["token"]
    print("   found: GlitchTip's organization, owner and read-only token in place")


def _reconnect(job: Job) -> None:
    """A recreated shared container leaves the workspaces' networks; join them again before the next deploy would."""
    for ws in job.config.workspaces:
        nets = (provision.network(job.config, ws.name), provision.SHARED)
        for network, shared in (nets, (provision.telemetry_network(job.config, ws.name), provision.TELEMETRY)):
            listing = ["docker", "network", "inspect", "--format", "{{range .Containers}}{{.Name}} {{end}}", network]
            found = job.host.read(listing)
            if found.returncode != 0:
                print(f"   found: no network {network} yet (its first deploy makes it)")
                continue
            for container in shared:
                if container not in found.stdout.split():
                    join = ["docker", "network", "connect", network, container]
                    job.host.change(f"join {container} to {network}", join)


def clean_up(job: Job) -> None:
    ensure_file(job.host, "/usr/local/bin/factory-gc", _wrapper("factory-gc"), owner="root:root", mode="755")
    line = f"17 4 * * 0 {job.config.server.admin} /usr/local/bin/factory-gc\n"
    text = "# The factory's weekly clean-up of old build cache and dangling images; it logs to /srv/factory/state.\n"
    ensure_file(job.host, CRON, text + line, owner="root:root", mode="644")


def heartbeat(job: Job) -> None:
    host, ping_file = job.host, str(heartbeat_mod.PING_FILE)
    ensure_file(host, "/usr/local/bin/factory-heartbeat", _wrapper("factory-heartbeat"), owner="root:root", mode="755")
    if job.ping:
        ensure_file(host, ping_file, job.ping + "\n", owner=f"root:{job.admin_group}", mode="640")
    elif remote_text(host, ping_file) is None:
        job.todo.append("give prod's Healthchecks.io ping address: run the set-up again with --healthchecks-ping <url>")
        return
    line = f"*/5 * * * * {job.config.server.admin} /usr/local/bin/factory-heartbeat\n"
    text = "# Prod's heartbeat: prod answers as prod, then Healthchecks.io hears from the server.\n"
    ensure_file(host, HEARTBEAT_CRON, text + line, owner="root:root", mode="644")


def tailnet(job: Job) -> None:
    host, name = job.host, job.config.server.tailnet
    done = host.read(["sudo", "-n", "tailscale", "serve", "status", "--json"])
    served = json.loads(done.stdout) if done.returncode == 0 and done.stdout.strip().startswith("{") else {}
    ports, web = served.get("TCP") or {}, served.get("Web") or {}
    missing: list[tuple[str, list[str]]] = []
    for port in SERVE_HTTPS:
        target = f"http://127.0.0.1:{port}"
        handler = ((web.get(f"{name}:{port}") or {}).get("Handlers") or {}).get("/") or {}
        if not ((ports.get(str(port)) or {}).get("HTTPS") is True and handler.get("Proxy") == target):
            missing.append((f"https {port}", ["sudo", "tailscale", "serve", "--bg", f"--https={port}", target]))
    for port in SERVE_TCP:
        if (ports.get(str(port)) or {}).get("TCPForward") != f"127.0.0.1:{port}":
            target = f"tcp://127.0.0.1:{port}"
            missing.append((f"tcp {port}", ["sudo", "tailscale", "serve", "--bg", f"--tcp={port}", target]))
    if not missing:
        print(f"   found: {name} serves " + ", ".join(str(p) for p in SERVE_HTTPS + SERVE_TCP))
        return
    print("   found: not served on the tailnet: " + ", ".join(what for what, _ in missing))
    if job.fresh:
        for what, argv in missing:
            host.change(f"serve {what} on the tailnet", ["sudo", "-n", *argv[1:]])
        return
    job.todo.append(
        "serve the shared services on the tailnet, on the server: " + "; ".join(shlex.join(a) for _, a in missing)
    )


def laptop(job: Job) -> None:
    config, path = job.config, job.home / "server.json"
    data = {
        "glitchtip": {
            "url": stack.public_url(config, stack.PORTS["glitchtip"]),
            "org": config.product.name,
            "token": job.token,
        },
    }
    export = _report_export(job)
    if export is not None:
        data["reports"] = export
    text = json.dumps(data, indent=2) + "\n"
    if path.exists() and path.read_text() == text and path.stat().st_mode & 0o777 == 0o600:
        print(f"   found: {path} up to date")
        return
    job.host.act(f"write {path} (0600) for the warden", lambda: _write_private(path, text))


def _report_export(job: Job) -> dict[str, str] | None:
    """The warden's way to prod's report export; None without [reports], or (with a to-do) until prod has its token."""
    reports, config = job.config.reports, job.config
    if reports is None:
        return None
    env_path = f"/srv/{config.product.name}/prod/env/{config.env.file}"
    found = remote_text(job.host, env_path)
    token = stack.parse_env(found[1], env_path).get(reports.token_key, "") if found else ""
    if not token or envfile.placeholders({reports.token_key: token}) or envfile.generate(token) is not None:
        job.todo.append(
            f"deploy prod, then run the set-up again: the warden reads users' reports with prod's {reports.token_key}, "
            f"which {env_path} does not hold yet"
        )
        return None
    url = stack.public_url(config, reports.port) + reports.path
    return {"url": url, "header": reports.header, "token": token}


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w") as out:
        out.write(text)
    os.replace(temp, path)
