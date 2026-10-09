"""`factory status` and `factory settings show`: what the factory is doing and how it is tuned, for anyone it serves."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from runtime.warden import codes, plan, registry, settings_save
from runtime.warden.context import Context


def status(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    sessions = registry.live(registry.read(ctx.places).values())
    rows = [{k: getattr(s, k) for k in ("id", "name", "role", "state", "task", "doing", "slot", "model")}
            for s in sorted(sessions, key=lambda s: s.started)]  # fmt: skip
    until = codes.locked_until(ctx.places, ctx.clock.now())
    return {
        "sessions": rows,
        "plan": plan.reading(ctx),
        "code_locked_until": None
        if until is None
        else datetime.fromtimestamp(until, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def settings_show(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    current = ctx.settings()
    return {
        "settings": current.tree(),
        "overrides": current.overrides,
        "version": settings_save.overrides_version(current.overrides),
    }
