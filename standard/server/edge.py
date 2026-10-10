"""The server's edge: one Caddy site file per workspace, one for the product's redirects, and a reload.

The edge (container factory-edge) imports every file in the sites folder. Each workspace's site sends its domain to
the loopback port its own stack publishes; the blocked paths answer 404 before anything reaches the stack.
"""

from __future__ import annotations

from pathlib import Path

from standard.config import Config
from standard.envfile import atomic_write
from standard.services.provision import Run, checked

EDGE = "factory-edge"
RELOAD = ["docker", "exec", EDGE, "caddy", "reload", "--config", "/etc/caddy/Caddyfile"]
HEADER = "# Written by the factory at every deploy; do not edit."


def site_text(config: Config, workspace: str) -> str:
    ws = config.workspace(workspace)
    blocked = [f"\trespond {path} 404" for path in config.edge.blocked_paths]
    return "\n".join(
        [
            HEADER,
            f"{ws.domain} {{",
            # Caddy matches the decoded, lowercased path with doubled slashes merged, so no spelling slips past.
            *blocked,
            f"\treverse_proxy 127.0.0.1:{ws.port} {{",
            # Without it, every reload cuts each proxied WebSocket at once.
            "\t\tstream_close_delay 30s",
            "\t}",
            "}",
            "",
        ]
    )


def routes_text(config: Config) -> str:
    lines = [HEADER]
    for route in config.edge.routes:
        lines += [f"{route.host} {{", f"\tredir https://{route.redirect}{{uri}} permanent", "}"]
    return "\n".join(lines) + "\n"


def write_sites(config: Config, sites_dir: Path, workspace: str) -> list[Path]:
    """Write the workspace's site file and the redirects file; return both paths."""
    name = config.product.name
    written = [sites_dir / f"{name}-{workspace}.caddy", sites_dir / f"{name}-routes.caddy"]
    # The edge reads these as its own user.
    atomic_write(written[0], site_text(config, workspace), 0o644)
    atomic_write(written[1], routes_text(config), 0o644)
    return written


def reload(run: Run) -> None:
    checked(run, RELOAD)
