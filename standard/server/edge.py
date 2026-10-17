"""The server's edge: one Caddy site file per workspace, one for the product's redirects, and a reload.

The edge (container factory-edge) imports every file in the sites folder. Each workspace's site sends its domain to
the loopback port its own stack publishes; the blocked paths answer 404 before anything reaches the stack. The
factory's install adds its dashboard's site there and, when the box opens staging to the world, staging's.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from standard.config_records import Config
from standard.envfile import atomic_write
from standard.services.provision import Run, checked

EDGE = "factory-edge"
SITES = Path("/srv/factory/edge/sites")
RELOAD = ["docker", "exec", EDGE, "caddy", "reload", "--config", "/etc/caddy/Caddyfile"]
HEADER = "# Written by the factory; do not edit."


def site(names: Sequence[str], port: int, blocked: Sequence[str] = (), bind: str | None = None) -> str:
    """One site: its names, its one address (``bind``), the paths that answer 404 first, and its loopback port."""
    return "\n".join(
        [
            HEADER,
            f"{', '.join(names)} {{",
            *([f"\tbind {bind}"] if bind else []),
            # Caddy matches the decoded, lowercased path with doubled slashes merged, so no spelling slips past.
            *(f"\trespond {path} 404" for path in blocked),
            f"\treverse_proxy 127.0.0.1:{port} {{",
            # Without it, every reload cuts each proxied WebSocket at once.
            "\t\tstream_close_delay 30s",
            "\t}",
            "}",
            "",
        ]
    )


def names(site_text: str) -> list[str]:
    return site_text.splitlines()[1].removesuffix(" {").split(", ")


def routes_text(config: Config) -> str:
    lines = [HEADER]
    for route in config.edge.routes:
        lines += [f"{route.host} {{", f"\tredir https://{route.redirect}{{uri}} permanent", "}"]
    return "\n".join(lines) + "\n"


def write_sites(config: Config, sites_dir: Path, workspace: str) -> list[Path]:
    name, ws = config.product.name, config.workspace(workspace)
    written = [sites_dir / f"{name}-{workspace}.caddy", sites_dir / f"{name}-routes.caddy"]
    # The edge reads these as its own user.
    atomic_write(written[0], site([ws.domain], ws.port, config.edge.blocked_paths), 0o644)
    atomic_write(written[1], routes_text(config), 0o644)
    return written


def reload(run: Run) -> None:
    checked(run, RELOAD)
