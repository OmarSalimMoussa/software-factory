"""Prod's heartbeat: every five minutes cron runs this on the server, as the admin user.

It asks prod's public health address, ``https://<[workspaces.prod].domain><[deploy].health>``, through the internet
and the edge like any visitor, and checks the answer names the workspace ``prod``. Then it checks in with
Healthchecks.io, or tells it at once that prod failed. When the check-ins stop, because the server or its network is
down, Healthchecks.io tells the owner.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path

from standard import network
from standard.config import ConfigError, parse
from standard.config_records import Config

PRODUCT_TOML = Path("/etc/software-factory/product.toml")
PING_FILE = Path("/etc/software-factory/heartbeat-ping")
TIMEOUT = 20  # seconds for prod's answer and for Healthchecks.io's; the check-in period is five minutes
Get = Callable[[str, bytes | None], bytes]


def _get(url: str, body: bytes | None) -> bytes:
    status, answer = network.call(url, TIMEOUT, data=body)
    if not 200 <= status < 300:
        raise OSError(f"{url} answered HTTP {status}")
    return answer


def prod_problem(config: Config, get: Get) -> str:
    """Why prod does not answer as prod; "" when it does."""
    url = f"https://{config.workspace('prod').domain}{config.deploy.health}"
    try:
        answer = json.loads(get(url, None))
    # telemetry: surfaced — a prod that does not answer with JSON is what `prod_problem` returns
    except (OSError, ValueError) as exc:
        return f"{url} did not answer with JSON ({type(exc).__name__})"
    workspace = answer.get("workspace") if isinstance(answer, dict) else None
    return "" if workspace == "prod" else f"{url} answered as workspace {workspace!r}, not 'prod'"


def beat(config: Config, ping: str, get: Get) -> int:
    problem = prod_problem(config, get)
    try:
        if problem:
            get(f"{ping}/fail", problem.encode())
        else:
            get(ping, None)
    except OSError as exc:
        print(f"heartbeat: Healthchecks.io did not take the check-in ({type(exc).__name__})", file=sys.stderr)
        return 1
    print(f"heartbeat: {problem or 'prod answers as prod'}")
    return 1 if problem else 0


def main() -> int:
    try:
        config = parse(PRODUCT_TOML)
        ping = PING_FILE.read_text(encoding="utf-8").strip()
    except (ConfigError, OSError) as exc:
        print(f"heartbeat: the server set-up's files cannot be read ({type(exc).__name__}: {exc})", file=sys.stderr)
        return 2
    return beat(config, ping, _get)


if __name__ == "__main__":
    sys.exit(main())
