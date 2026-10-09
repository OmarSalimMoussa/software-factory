"""The owner's 6-digit code, checked inside each coded action, and the devices one code admitted to the dashboard.

A code is accepted once: the step it belongs to is remembered and no code of that step or an earlier one is accepted
again. ``dashboard.wrong_codes_to_lock`` wrong codes within ``dashboard.lock_minutes`` lock coded actions for that
long; a lock never blocks pausing or stopping the factory (``lock_exempt``). A refused action is not a wrong code:
only the code itself is counted here.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runtime.shared import store, totp
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused
from runtime.warden.context import Context

_lock = threading.Lock()


def _devices_path(places: Places) -> Path:
    return places.state / "devices.json"


def _read_lock(places: Places) -> dict[str, Any]:
    raw = store.read_json_or(places.code_lock, {"last_step": None, "wrong": [], "locked_until": None})
    if not isinstance(raw, dict) or set(raw) != {"last_step", "wrong", "locked_until"}:
        raise store.StoreError(f"{places.code_lock}: needs last_step, wrong and locked_until")
    return raw


def locked_until(places: Places, now: float) -> float | None:
    """When coded actions unlock, or None when they are not locked."""
    until = _read_lock(places)["locked_until"]
    return until if isinstance(until, int | float) and until > now else None


def _clock_time(when: float) -> str:
    return datetime.fromtimestamp(when).astimezone().strftime("%H:%M")


def _step_of(secret: str, code: str, now: float) -> int | None:
    """The 30-second step whose code this is: now, or the step just before or after for a drifting phone."""
    if len(code) != totp.DIGITS or not code.isdigit():
        return None
    counter = int(now // totp.STEP_S)
    for step in (counter - 1, counter, counter + 1):
        if hmac.compare_digest(totp.code_at(secret, step), code):
            return step
    return None


def check_code(ctx: Context, code: object, *, lock_exempt: bool = False) -> None:
    """Accept the owner's code for one coded action, or raise Refused saying why it was not accepted."""
    settings = ctx.settings()
    limit = settings.get_int("dashboard.wrong_codes_to_lock")
    window = settings.get_int("dashboard.lock_minutes") * 60
    now = ctx.clock.now()
    with _lock:
        state = _read_lock(ctx.places)
        until = state["locked_until"]
        locked = isinstance(until, int | float) and until > now
        if locked and not lock_exempt:
            raise Refused("code-locked", f"coded actions are locked until {_clock_time(until)} after wrong codes")
        if not isinstance(code, str) or not code:
            raise Refused("code", "this action needs the 6-digit code from your authenticator app")
        step = _step_of(ctx.places.totp_secret.read_text().strip(), code, now)
        last = state["last_step"]
        if step is not None and isinstance(last, int) and step <= last:
            raise Refused("code", "that code was already used; enter the next one your app shows")
        if step is None:
            wrong = [t for t in state["wrong"] if now - t < window] + [now]
            if locked:
                state["wrong"] = wrong
                store.write_json(ctx.places.code_lock, state, mode=0o600)
                raise Refused("code", "wrong code")
            if len(wrong) >= limit:
                state.update(wrong=[], locked_until=now + window)
                store.write_json(ctx.places.code_lock, state, mode=0o600)
                raise Refused("code-locked", f"wrong code; coded actions are locked until {_clock_time(now + window)}")
            state["wrong"] = wrong
            store.write_json(ctx.places.code_lock, state, mode=0o600)
            left = limit - len(wrong)
            raise Refused("code", f"wrong code, {left} {'try' if left == 1 else 'tries'} left")
        state.update(last_step=step, wrong=[])
        store.write_json(ctx.places.code_lock, state, mode=0o600)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _read_devices(places: Places) -> dict[str, float]:
    raw = store.read_json_or(_devices_path(places), {})
    if not isinstance(raw, dict) or not all(isinstance(v, int | float) for v in raw.values()):
        raise store.StoreError(f"{_devices_path(places)}: needs each device's expiry")
    return raw


def admit_device(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """Admit the device that sent a right code: a fresh token the dashboard keeps in its owner cookie."""
    now = ctx.clock.now()
    expires = now + ctx.settings().get_int("dashboard.device_days") * 86400
    token = secrets.token_urlsafe(32)
    with _lock:
        devices = {digest: until for digest, until in _read_devices(ctx.places).items() if until > now}
        devices[_digest(token)] = expires
        store.write_json(_devices_path(ctx.places), devices, mode=0o600)
    return {"token": token, "expires": datetime.fromtimestamp(expires, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}


def verify_device(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """Whether a device token is still admitted: ``ok`` until its expiry, ``expired`` after it or when unknown."""
    token = args.get("token")
    if not isinstance(token, str) or not token:
        raise Refused("device", "send the device token from the owner cookie")
    until = _read_devices(ctx.places).get(_digest(token))
    if until is None or until <= ctx.clock.now():
        return {"device": "expired"}
    return {"device": "ok", "expires": datetime.fromtimestamp(until, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}
