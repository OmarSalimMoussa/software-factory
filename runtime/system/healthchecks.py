"""Healthchecks.io, off the laptop: it tells the owner when the warden or the orchestrator goes quiet. Watching
prod is the product's own.

The installer creates the two checks with the owner's API key (``--healthchecks``) and keeps their ping URLs in the
warden's secrets. Before the laptop sleeps or the warden stops on purpose, both checks are paused; their next
check-in resumes them.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Any

from runtime.shared.paths import WARDEN_USER
from runtime.shared.settings import Settings
from runtime.system import layout
from runtime.system.job import Install, refuse

API = "https://healthchecks.io/api/v3/checks/"


def keys_path(state: Path) -> Path:
    return layout.secrets(state) / "healthchecks.json"


def periods(settings: Settings) -> dict[str, tuple[int, int]]:
    """Each check's expected period and grace, in seconds: how often it is pinged, and how late it may be."""
    grace = settings.get_int("install.healthchecks_grace_minutes") * 60
    return {
        "warden": (settings.get_int("watch.probe_seconds"), grace),
        "orchestrator": (settings.get_int("watch.loop_minutes") * 60, grace),
    }


def _call(url: str, key: str, body: dict[str, Any] | None, timeout: int) -> dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url, data=data, method="POST", headers={"X-Api-Key": key, "Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - the fixed Healthchecks.io API
        answer = json.loads(response.read() or b"{}")
    if not isinstance(answer, dict):
        raise ValueError("Healthchecks.io answered with something other than an object")
    return answer


def create(ctx: Install) -> None:
    host = ctx.host
    if not ctx.healthchecks:
        host.say("skipped: run the install with --healthchecks to create them")
        return
    settings = ctx.need_settings()
    key = host.ask(
        "Healthchecks.io project API key (read-write): ", secret=True,
        hint="Make it in your Healthchecks.io project's Settings, under API Access.",
        link="https://healthchecks.io/docs/api/",
    )  # fmt: skip
    pings: dict[str, str] = {}
    for name, (timeout, grace) in periods(settings).items():
        if host.dry:
            host.say(f"would create the check '{name}' ({timeout} s, grace {grace} s)")
            continue
        body = {"name": f"software factory: {name}", "timeout": timeout, "grace": grace, "unique": ["name"],
                "channels": "*"}  # fmt: skip
        try:
            pings[name] = str(_call(API, key, body, settings.get_int("watch.http_timeout_seconds"))["ping_url"])
        except (OSError, ValueError, KeyError) as exc:
            host.say(f"Healthchecks.io refused the {name} check: {type(exc).__name__}: {exc}")
            raise refuse(
                "Healthchecks.io did not take the API key: make a read-write key in your project's settings, "
                "then install again"
            ) from exc
    record = {"api_key": key, "pings": pings, "checks": {n: url.rsplit("/", 1)[-1] for n, url in pings.items()}}
    host.write(keys_path(ctx.places.state), json.dumps(record) + "\n", mode=0o600, owner=WARDEN_USER)


def pause(state: Path, settings: Settings) -> list[str]:
    """Pause every check; the problems, one per check that could not be paused."""
    try:
        record = json.loads(keys_path(state).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    timeout = settings.get_int("watch.http_timeout_seconds")
    problems = []
    for name, uuid in record["checks"].items():
        try:
            _call(f"{API}{uuid}/pause", record["api_key"], None, timeout)
        except (OSError, ValueError) as exc:
            problems.append(f"{name}: {type(exc).__name__}")
    return problems
