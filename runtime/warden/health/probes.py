"""Probing staging's and prod's health addresses, and telling a failure of the host from a failure on this side.

Each address is https://<domain><[deploy].health>, from factory.toml in the product's main checkout, read at every
probe: only a person changes that file. A probe asks it for a JSON object naming the running commit as build_commit.
A failure counts toward an outage only when the host itself answered wrongly (an HTTP error, a redirect or an answer
with no build_commit) or timed out, and the control probe reached GitHub at that moment. A failure to reach the host
at all (no name, refused, unreachable, TLS) is this side's network and never counts, and neither is a factory.toml
that cannot be read: the probe then has no address, says why, and never guesses one.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from runtime.queue import files
from runtime.shared import models, network, store
from runtime.shared.models import Probe
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused
from runtime.warden.context import Context
from runtime.warden.delivery import deploys
from standard import config

STAGING, PROD = "staging", "prod"
ENVS = (STAGING, PROD)
_MAX_BODY = 64 * 1024  # a health answer is a few hundred bytes
_TAIL_CHUNK = 16 * 1024
_SHA = re.compile(r"^[0-9a-f]{7,40}$")


class NetworkFailure(Exception):
    """The request never reached the host; the message names the failure's class."""


class NoAddress(Exception):
    """factory.toml in the main checkout cannot give an environment's health address; the message says why."""


@dataclass(frozen=True, slots=True)
class Answer:
    status: int
    body: bytes


# Raises TimeoutError when the host does not answer in time and NetworkFailure when it cannot be reached.
Fetch = Callable[[str, float], Answer]


def http_get(url: str, timeout: float) -> Answer:
    """A health address answers itself: a redirect (a captive portal, a moved host) is an answer, not a hop."""
    request = urllib.request.Request(url, headers={"Accept": "application/json", "Cache-Control": "no-cache"})
    try:
        found = network.call(request, timeout, limit=_MAX_BODY)
    except TimeoutError:
        raise
    except OSError as exc:
        raise NetworkFailure(network.failure_class(exc)) from exc
    return Answer(found.status, found.body)


def address(ctx: Context, env: str) -> str:
    """``env``'s health address, from factory.toml in the main checkout (repo.path), read without following a
    symlink as GOAL.md is."""
    settings = ctx.settings()
    repo = Path(settings.get_str("repo.path"))
    try:
        raw = files.read_repo_file(repo, config.FILE, settings.get_int("queue.max_upload_kib") * 1024)
        product = config.parse_text(raw.decode(), repo / config.FILE)
    except FileNotFoundError as exc:
        raise NoAddress(f"{config.FILE} is missing from {repo}") from exc
    except config.ConfigError as exc:
        raise NoAddress(f"{config.FILE} is not usable: {'; '.join(exc.problems)}") from exc
    except Refused as exc:
        raise NoAddress(exc.reason) from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise NoAddress(f"{config.FILE} cannot be read ({type(exc).__name__})") from exc
    return f"https://{product.workspace(env).domain}{product.deploy.health}"


def take(ctx: Context, env: str, fetch: Fetch) -> Probe:
    """Probe one environment now and record it in probes.jsonl."""
    timeout = float(ctx.settings().get_int("watch.http_timeout_seconds"))
    at = ctx.clock.iso()
    started = time.monotonic()
    try:
        answer = fetch(address(ctx, env), timeout)
    # telemetry: surfaced — the `probe` records no health address, not counted, for the dashboard to draw
    except NoAddress as exc:
        probe = Probe(at, env, False, "", None, f"no health address: {exc}; not counted")
    # telemetry: surfaced — the `probe` records no answer in time, for the dashboard to draw
    except TimeoutError:
        probe = _failed(ctx, at, env, _ms(started), f"no answer within {timeout:g} s")
    # telemetry: surfaced — the `probe` records the host unreachable from here, not counted
    except NetworkFailure as exc:
        probe = Probe(at, env, False, "", None, f"could not reach the host from here ({exc}); not counted")
    else:
        commit = _build_commit(answer.body) if answer.status == 200 else None
        if commit is not None:
            probe = Probe(at, env, True, commit, _ms(started))
        elif answer.status != 200:
            probe = _failed(ctx, at, env, _ms(started), f"HTTP {answer.status}")
        else:
            probe = _failed(ctx, at, env, _ms(started), "the answer names no build_commit")
    store.append_jsonl(ctx.places.probes, models.dump(probe))
    if probe.ok:
        deploys.answered(ctx.places, env, probe.commit)
    return probe


def _failed(ctx: Context, at: str, env: str, ms: int, error: str) -> Probe:
    if ctx.github.reachable():
        return Probe(at, env, False, "", ms, error, counts=True)
    return Probe(at, env, False, "", ms, f"{error}; GitHub did not answer either, so it is not counted")


def _ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def _build_commit(body: bytes) -> str | None:
    try:
        parsed = json.loads(body)
    # telemetry: expected — a body that is not JSON names no build: the probe records none
    except ValueError:
        return None
    commit = parsed.get("build_commit") if isinstance(parsed, dict) else None
    return commit if isinstance(commit, str) and _SHA.match(commit) else None


def recent(places: Places, env: str, n: int) -> list[Probe]:
    """The last ``n`` probes of ``env``, oldest first, read from the end of probes.jsonl."""
    found: list[Probe] = []
    try:
        fh = places.probes.open("rb")
    # telemetry: expected — no probes yet: nothing has been probed on this machine
    except FileNotFoundError:
        return []
    with fh:
        pos = fh.seek(0, os.SEEK_END)
        carry = b""
        while pos > 0 and len(found) < n:
            step = min(_TAIL_CHUNK, pos)
            pos -= step
            fh.seek(pos)
            lines = (fh.read(step) + carry).split(b"\n")
            carry = lines.pop(0) if pos > 0 else b""
            for line in reversed(lines):
                if not line.strip():
                    continue
                try:
                    probe = models.load(Probe, json.loads(line))
                except ValueError as exc:
                    raise store.StoreError(f"{places.probes}: {exc}") from exc
                if probe.env == env:
                    found.append(probe)
                    if len(found) == n:
                        break
    return found[::-1]
