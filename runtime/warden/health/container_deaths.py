"""Containers that stopped when nothing asked, on a slot, in a workspace (prod) or among the shared services: each is
reported to the factory's GlitchTip project once.

Each such service restarts unless stopped, so Docker brings back one that stopped on its own and counts it; a stop
someone asked for never counts. The stack unit lists each count every ``watch.container_deaths_minutes``, and each
rise is reported. Docker's event history keeps only its newest 256 events, under a minute here: too short a span.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from runtime.shared import store
from runtime.shared.paths import Places
from runtime.slots import leases
from runtime.warden.context import Context
from runtime.warden.health import probes
from standard.config_records import Config
from standard.services import stack

log = logging.getLogger("factory.warden")

_NO_SLOT = 0
_SESSION = "container-deaths"


class CountsError(Exception):
    """The stack unit could not list the containers' restart counts."""


def _counts(places: Places) -> Path:
    return places.state / "container-restarts.json"


def whose(product: Config, container: dict[str, Any]) -> str:
    if container["slot"]:
        return f"slot {container['slot']}"
    homes = {product.project(ws.name): ws.name for ws in product.workspaces}
    return (homes | {stack.PROJECT: "shared services"}).get(container["project"], "")


def run_pass(ctx: Context) -> int:
    """List the counts when due, and report each container that stopped on its own since; how many were reported."""
    now = int(ctx.clock.now())
    last = store.read_json_or(_counts(ctx.places), None)
    if last is not None and now - last["at"] < ctx.settings().get_int("watch.container_deaths_minutes") * 60:
        return 0
    done = ctx.launcher.run_stack(_NO_SLOT, [leases.RESTARTS], _SESSION)
    if done.exit != 0:
        raise CountsError(f"the stack unit could not list the containers' restart counts (exit {done.exit})")
    product = probes.product(ctx)
    lines = Path(done.log).read_text(encoding="utf-8").splitlines()
    counts: dict[str, int] = {}
    reported = 0
    for container in (json.loads(line) for line in lines if line.startswith("{")):
        where = whose(product, container)
        if not where:
            continue
        counts[container["id"]] = container["restarts"]
        stopped = container["restarts"] - last["restarts"].get(container["id"], 0) if last is not None else 0
        if stopped > 0:
            times = "once" if stopped == 1 else f"{stopped} times"
            log.error("warden: %s container %s stopped %s when nothing asked; Docker restarted it", where,
                      container["name"], times)  # fmt: skip
            reported += 1
    store.write_json(_counts(ctx.places), {"at": now, "restarts": counts})
    return reported
