"""`factory slot …`: the warden runs the product's scripts/dev for the caller's own slot (factory spec, "Slots").

The slot is always worked out from the caller's lease, never from an argument: slot 0 is the orchestrator's, an
implementer has the slot it claimed, and its testers share it. A stack command before ``claim`` is refused. The
answer carries scripts/dev's exit status and the redacted tail of its output; the full log stays the owner's.

A tester only reads its slot, signs in and opens its desktop: seeds and the stack are its parent's. One tester holds
a slot's desktop at a time, from its `desktop up` until it hands back (its row retires) or its parent takes the
desktop down; another tester's `desktop up` waits, so no one's new password cuts off a tester mid-step.
"""

from __future__ import annotations

import re
from typing import Any

from runtime.shared import store
from runtime.shared.models import RETIRED, Session
from runtime.shared.protocol import Refused, ok
from runtime.slots import leases
from runtime.warden import registry
from runtime.warden.context import Context, StackResult

ORCH, IMPL = "orchestrator", "implementer"
WORD = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$")
EMAIL = re.compile(r"^[A-Za-z0-9._+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}$")
# Verbs a tester may run on its implementer's slot; the rest change the stack and are the implementer's own.
TESTER_OPS = frozenset({"list", "which", "sign-in", "desktop"})


def _session(ctx: Context) -> Session:
    session = registry.read(ctx.places).get(ctx.caller.session)
    if session is None or session.state == RETIRED:
        raise Refused("slot", "only a live factory session has a slot")
    return session


def _slot(ctx: Context, session: Session) -> int:
    if session.role == ORCH:
        return 0
    lease = leases.held_by(ctx.places, ctx.settings(), session.name)
    if lease is None:
        raise Refused("slot", "claim a slot first: factory slot claim")
    return lease.slot


def _words(values: object, what: str) -> list[str]:
    if not isinstance(values, list) or not all(isinstance(v, str) and WORD.match(v) for v in values):
        raise Refused("args", f"{what} are plain words, none starting with a dash")
    return list(values)


def _email(value: object) -> str:
    if not isinstance(value, str) or not EMAIL.match(value):
        raise Refused("args", "name a test address such as tester-1@example.com")
    return value


def _answer(n: int, result: StackResult) -> dict[str, Any]:
    return ok(slot=n, exit=result.exit, tail=result.tail)


def slot(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    op = args.get("op")
    session = _session(ctx)
    if ctx.caller.agent_id and op not in TESTER_OPS:
        raise Refused("slot", f"a tester may run {', '.join(sorted(TESTER_OPS))} on its slot, not {op}")
    settings = ctx.settings()
    if op == "claim":
        if session.role != IMPL:
            raise Refused("slot", "only an implementer claims a slot; slot 0 is the orchestrator's")
        n = leases.claim(ctx, session.name, session.worktree, str(args.get("note", "")))
        return ok(slot=n, port=leases.port(settings, n))
    if op == "list":
        return ok(slots=_list(ctx, session))
    if op == "free":
        return _free(ctx, session, args.get("slot"))
    n = _slot(ctx, session)
    if op == "which":
        return ok(
            slot=n,
            port=leases.port(settings, n),
            timeout_seconds=settings.get_int("slots.api_timeout_seconds"),
            max_kib=settings.get_int("slots.api_max_kib"),
        )
    if op == "release":
        failed = leases.empty(ctx, n, session.id)
        return ok(slot=n, released=not failed, **({"failed": failed} if failed else {}))
    verb = _verb(op, args)
    if op == "desktop":
        _desktop_lease(ctx, n, verb)
    result = ctx.launcher.run_stack(n, verb, session.id)
    return _answer(n, result)


def _desktops(ctx: Context) -> dict[str, str]:
    raw = store.read_json_or(ctx.places.state / "desktops.json", {})
    if not isinstance(raw, dict) or not all(isinstance(v, str) for v in raw.values()):
        raise store.StoreError(f"{ctx.places.state / 'desktops.json'}: not a holder per slot")
    return raw


def _desktop_lease(ctx: Context, n: int, verb: list[str]) -> None:
    """A tester's `desktop up` takes the slot's desktop while no other live tester holds it; its parent's `desktop
    down` frees it."""
    agent = ctx.caller.agent_id
    if agent and verb[1] != "up":
        raise Refused("slot", "a tester opens the desktop; its implementer takes it down")
    with ctx.lock:
        held = _desktops(ctx)
        if not agent:
            held.pop(str(n), None)
        else:
            holder = registry.read(ctx.places).get(held.get(str(n), ""))
            if holder is not None and holder.id != agent and holder.state != RETIRED:
                raise Refused("desktop", f"slot {n}'s desktop is {holder.name}'s until it hands back; test what "
                                         "needs no desktop first, then try again")  # fmt: skip
            held[str(n)] = agent
        store.write_json(ctx.places.state / "desktops.json", held)


def _verb(op: object, args: dict[str, Any]) -> list[str]:
    """The scripts/dev command line for one slot operation, from checked words only."""
    if op == "up":
        return ["up", *_words(args.get("services", []), "services")]
    if op == "down":
        return ["down"]
    if op == "sign-in":
        return ["sign-in", _email(args.get("email"))]
    if op == "seed":
        words = _words(args.get("seed", []), "seed words")
        if words[:1] == ["save"] and len(words) == 3 or words == ["check"]:
            return ["seed", *words]
        if words[:1] == ["load"] and len(words) == 2:
            return ["seed", *words, *(["--as", _email(args["as"])] if args.get("as") else [])]
        raise Refused("args", "seed save <id> <name> | seed load <name> [--as <email>] | seed check")
    if op == "desktop":
        words = _words(args.get("desktop", []), "desktop words")
        if words == ["down"]:
            return ["desktop", "down"]
        if words[:1] == ["up"] and len(words) == 2:
            return ["desktop", "up", _email(words[1])]
        raise Refused("args", "desktop up <email> | desktop down")
    raise Refused("args", f"{op!r} is not a slot verb: claim, up, down, release, free, list, sign-in, seed, desktop")


def _list(ctx: Context, session: Session) -> list[dict[str, Any]]:
    settings = ctx.settings()
    rows: list[dict[str, Any]] = [
        {"slot": 0, "port": leases.port(settings, 0), "holder": "the main checkout", "mine": session.role == ORCH}
    ]
    for n in leases.numbers(ctx.places, settings):
        lease = leases.read(ctx.places, n)
        rows.append(
            {
                "slot": n,
                "port": leases.port(settings, n),
                "holder": lease.holder if lease else "",
                "since": lease.since if lease else "",
                "note": lease.note if lease else "",
                "mine": bool(lease) and lease is not None and lease.holder == session.name,
            }
        )
    return rows


def _free(ctx: Context, session: Session, n: object) -> dict[str, Any]:
    """The orchestrator empties and frees a finished session's slot; a live holder keeps it."""
    if session.role != ORCH:
        raise Refused("slot", "slot free is the orchestrator's, for finished work")
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise Refused("args", "name the slot to free: factory slot free <n>")
    lease = leases.read(ctx.places, n)
    if lease is None:
        raise Refused("slot", f"slot {n} is already free")
    holder = registry.by_name(registry.read(ctx.places).values(), lease.holder)
    if holder is not None:
        raise Refused("slot", f"slot {n} is held by {lease.holder}, which is {holder.state}; it releases its own slot")
    failed = leases.empty(ctx, n, session.id)
    return ok(slot=n, released=not failed, **({"failed": failed} if failed else {}))
