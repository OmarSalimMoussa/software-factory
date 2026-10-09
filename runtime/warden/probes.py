"""Probing staging's and prod's health addresses, and telling a failure of the host from a failure on this side.

A probe asks the address repo.staging_health or repo.prod_health names for a JSON object naming the running commit
as build_commit. A failure counts toward an outage only when the host itself answered wrongly (an HTTP error, a
redirect or an answer with no build_commit) or timed out, and the control probe reached GitHub at that moment. A
failure to reach the host at all (no name, refused, unreachable, TLS) is this side's network and never counts.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from runtime.shared import models, store
from runtime.shared.models import Probe
from runtime.shared.paths import Places
from runtime.warden.context import Context

STAGING, PROD = "staging", "prod"
ENVS = (STAGING, PROD)
_MAX_BODY = 64 * 1024  # a health answer is a few hundred bytes
_TAIL_CHUNK = 16 * 1024
_SHA = re.compile(r"^[0-9a-f]{7,40}$")


class NetworkFailure(Exception):
    """The request never reached the host; the message names the failure's class."""


@dataclass(frozen=True, slots=True)
class Answer:
    status: int
    body: bytes


# Raises TimeoutError when the host does not answer in time and NetworkFailure when it cannot be reached.
Fetch = Callable[[str, float], Answer]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A health address answers itself; a redirect (a captive portal, a moved host) is an answer, not a hop."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def http_get(url: str, timeout: float) -> Answer:
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(url, headers={"Accept": "application/json", "Cache-Control": "no-cache"})
    try:
        with opener.open(request, timeout=timeout) as response:
            return Answer(response.status, response.read(_MAX_BODY))
    except urllib.error.HTTPError as exc:
        return Answer(exc.code, b"")
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise TimeoutError(url) from exc
        raise NetworkFailure(type(exc.reason).__name__) from exc
    except TimeoutError:
        raise
    except (OSError, http.client.HTTPException) as exc:
        raise NetworkFailure(type(exc).__name__) from exc


def take(ctx: Context, env: str, fetch: Fetch) -> Probe:
    """Probe one environment now and record it in probes.jsonl."""
    settings = ctx.settings()
    url = settings.get_str(f"repo.{env}_health")
    timeout = float(settings.get_int("watch.http_timeout_seconds"))
    at = ctx.clock.iso()
    started = time.monotonic()
    try:
        answer = fetch(url, timeout)
    except TimeoutError:
        probe = _failed(ctx, at, env, _ms(started), f"no answer within {timeout:g} s")
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
    except ValueError:
        return None
    commit = parsed.get("build_commit") if isinstance(parsed, dict) else None
    return commit if isinstance(commit, str) and _SHA.match(commit) else None


def recent(places: Places, env: str, n: int) -> list[Probe]:
    """The last ``n`` probes of ``env``, oldest first, read from the end of probes.jsonl."""
    found: list[Probe] = []
    try:
        fh = places.probes.open("rb")
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
