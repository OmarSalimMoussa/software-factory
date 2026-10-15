"""One request in, one answer out: the verb's table entry, the caller's role and socket, the code, then the handler.

The caller was already established from the socket's peer (server.py). A request carrying ``idempotency_key`` that was
answered before gets the stored answer and runs nothing; a retry that arrives while the first is still running waits
for it. Only answers of actions that ran are stored: a refusal changed nothing, so a retry may run.
"""

from __future__ import annotations

import importlib
import logging
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from runtime.shared import protocol, store
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused
from runtime.shared.settings import Settings
from runtime.warden.context import Caller, CheckPlaces, Clock, Context, GitHub, Launcher
from runtime.warden.owner import codes
from runtime.warden.sessions import subagents
from runtime.warden.verbs import VERBS, Verb

log = logging.getLogger("factory.warden")

AGENT_SOCKET, OWNER_SOCKET = "agent", "owner"
_KEY = re.compile(r"^[A-Za-z0-9_-]{8,128}$")

Handler = Callable[[Context, dict[str, Any], bytes | None], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class Doors:
    """Everything a handler's Context holds besides its caller, built once in the composition root."""

    places: Places
    settings: Callable[[], Settings]
    clock: Clock
    launcher: Launcher
    github: GitHub
    post_event: Callable[[str, dict[str, Any]], None]
    lock: threading.RLock
    check_places: CheckPlaces

    def context(self, caller: Caller) -> Context:
        return Context(self.places, self.settings, caller, self.clock, self.launcher, self.github,
                       self.post_event, self.lock, self.check_places)  # fmt: skip


def resolve(verbs: Mapping[str, Verb]) -> dict[str, Handler]:
    """Every verb's handler; one error lists every handler that cannot be found."""
    found: dict[str, Handler] = {}
    problems: list[str] = []
    for name, verb in verbs.items():
        module_name, _, attr = verb.handler.partition(":")
        try:
            found[name] = cast(Handler, getattr(importlib.import_module(module_name), attr))
        # telemetry: surfaced — each missing handler is one of the `problems` the start-up raises
        except (ImportError, AttributeError) as exc:
            problems.append(f"{name} -> {verb.handler} ({type(exc).__name__})")
    if problems:
        raise LookupError("verb handlers missing: " + "; ".join(problems))
    return found


class Idempotency:
    """Answers by idempotency key, kept for ``dashboard.idempotency_hours`` in the warden's state."""

    def __init__(self, places: Places, settings: Callable[[], Settings], clock: Clock) -> None:
        self._path = places.state / "idempotency.json"
        self._settings, self._clock = settings, clock
        self._lock = threading.Lock()
        self._running: dict[str, threading.Event] = {}

    def _read(self) -> dict[str, Any]:
        raw = store.read_json_or(self._path, {})
        if not isinstance(raw, dict):
            raise store.StoreError(f"{self._path}: not an object")
        return raw

    def claim(self, key: str, verb: str) -> dict[str, Any] | None:
        """The stored answer for ``key``, or None when this request is the one to run it."""
        while True:
            with self._lock:
                stored = self._read().get(key)
                if stored is not None:
                    if stored["verb"] != verb:
                        raise Refused("idempotency", "that idempotency key belongs to another action; send a new one")
                    return cast(dict[str, Any], stored["answer"])
                running = self._running.get(key)
                if running is None:
                    self._running[key] = threading.Event()
                    return None
            running.wait()

    def settle(self, key: str, verb: str, answer: dict[str, Any] | None) -> None:
        """Store the answer of an action that ran (None: it was refused and is not stored), and wake any retry."""
        with self._lock:
            try:
                if answer is not None:
                    now = self._clock.now()
                    keep = self._settings().get_int("dashboard.idempotency_hours") * 3600
                    kept = {k: v for k, v in self._read().items() if now - v["at"] < keep}
                    kept[key] = {"verb": verb, "at": now, "answer": answer}
                    store.write_json(self._path, kept, mode=0o600)
            finally:
                self._running.pop(key).set()


class Dispatcher:
    def __init__(self, doors: Doors, handlers: Mapping[str, Handler], verbs: Mapping[str, Verb] = VERBS) -> None:
        self.doors, self.handlers, self.verbs = doors, handlers, verbs
        self.idempotency = Idempotency(doors.places, doors.settings, doors.clock)

    def answer(self, caller: Caller, socket: str, line: bytes) -> dict[str, Any]:
        try:
            name, args, data, agent_id = protocol.parse_request(line)
            if agent_id:
                caller = subagents.acting_as(self.doors.places, caller, agent_id)
            verb = self._allowed(caller, socket, name)
        # telemetry: surfaced — the caller's answer is `refused`, with the rule and the reason
        except Refused as exc:
            return protocol.refused(exc.rule, exc.reason, exc.detail)
        # telemetry: surfaced — the caller's answer is `refused`: its upload was not base64
        except ValueError:
            return protocol.refused("protocol", "the request's bytes_b64 is not base64")
        key = args.get("idempotency_key")
        if key is None:
            return self._run(caller, name, verb, args, data)[0]
        return self._once(key, caller, name, verb, args, data)

    def _once(
        self, key: object, caller: Caller, name: str, verb: Verb, args: dict[str, Any], data: bytes | None
    ) -> dict[str, Any]:
        """The verb run once for its idempotency key: asked again with the key, it answers what it answered then."""
        if not isinstance(key, str) or not _KEY.match(key):
            return protocol.refused("protocol", "idempotency_key must be 8 to 128 letters, digits, - or _")
        try:
            stored = self.idempotency.claim(key, name)
        # telemetry: surfaced — the caller's answer is `refused`, with the rule and the reason
        except Refused as exc:
            return protocol.refused(exc.rule, exc.reason, exc.detail)
        if stored is not None:
            return stored
        reply, ran = protocol.refused("failed", "the warden stopped this action"), False
        try:
            reply, ran = self._run(caller, name, verb, args, data)
        finally:
            self.idempotency.settle(key, name, reply if ran else None)
        return reply

    def _allowed(self, caller: Caller, socket: str, name: str) -> Verb:
        verb = self.verbs.get(name)
        if verb is None:
            raise Refused("verb", f"the warden has no verb {name!r}; run factory --help for the commands")
        if verb.owner_socket != (socket == OWNER_SOCKET):
            where = "the dashboard or the owner's socket" if verb.owner_socket else "the factory command"
            raise Refused("socket", f"{name} is asked through {where}")
        if caller.role not in verb.roles:
            who = caller.role or "a process the warden did not start"
            raise Refused("role", f"{who} may not {name}; it is for {', '.join(sorted(verb.roles))}")
        if caller.agent_id and caller.agent_kind not in verb.kinds:
            raise Refused("role", f"a {caller.agent_kind} subagent may not {name}; report to your parent instead")
        return verb

    def _run(
        self, caller: Caller, name: str, verb: Verb, args: dict[str, Any], data: bytes | None
    ) -> tuple[dict[str, Any], bool]:
        """The answer, and whether the action ran (a refusal did not)."""
        ctx = self.doors.context(caller)
        try:
            if verb.coded:
                codes.check_code(ctx, args.get("code"))
            result = self.handlers[name](ctx, args, data)
        # telemetry: surfaced — the caller's answer is `refused`, with the rule and the reason
        except Refused as exc:
            return protocol.refused(exc.rule, exc.reason, exc.detail), False
        except Exception as exc:
            log.exception("warden: %s failed for %s %s (%s)", name, caller.kind, caller.session, type(exc).__name__)
            return protocol.refused(
                "failed", f"the warden failed ({type(exc).__name__}); its log has the details"
            ), True
        return {**result, "ok": True}, True
