"""Operations: the owner's long actions (a settings save, a push to prod, a pause), with steps the dashboard shows.

Each operation is one file, ops/<id>.json in the warden's state, rewritten as its steps move, so its progress survives
a reload of the page and a restart of the warden. An operation that was running when the warden stopped is marked
failed at the next start, at the step it had reached.
"""

from __future__ import annotations

import logging
import re
import secrets
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runtime.shared import models, store
from runtime.shared.models import Operation
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused
from runtime.warden.context import Clock, Context

log = logging.getLogger("factory.warden")

WAITING, RUNNING, DONE, FAILED = "waiting", "running", "done", "failed"
_ID = re.compile(r"^op-[0-9]{14}-[0-9a-f]{8}$")
_lock = threading.Lock()
_threads: dict[str, threading.Thread] = {}

Step = tuple[str, Callable[[], str]]


class StepError(Exception):
    """A step failed for a reason outside the owner's action (the network, a command); ``detail`` says which."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


def _path(places: Places, op_id: str) -> Path:
    return places.state / "ops" / f"{op_id}.json"


def _read(places: Places, op_id: str) -> Operation:
    return models.load(Operation, store.read_json(_path(places, op_id)))


def _write(places: Places, op: Operation) -> None:
    store.write_json(_path(places, op.id), models.dump(op), mode=0o600)


def start(ctx: Context, kind: str, steps: Sequence[str]) -> str:
    stamp = datetime.fromtimestamp(ctx.clock.now(), UTC).strftime("%Y%m%d%H%M%S")
    op_id = f"op-{stamp}-{secrets.token_hex(4)}"
    op = Operation(
        id=op_id,
        kind=kind,
        started=ctx.clock.iso(),
        steps=[{"text": text, "state": WAITING, "detail": ""} for text in steps],
        state=RUNNING,
    )
    with _lock:
        _write(ctx.places, op)
    return op_id


def step(ctx: Context, op_id: str, index: int, state: str, detail: str = "") -> None:
    with _lock:
        op = _read(ctx.places, op_id)
        op.steps[index] = {"text": op.steps[index]["text"], "state": state, "detail": detail}
        _write(ctx.places, op)


def complete(ctx: Context, op_id: str, index: int, detail: str, result: dict[str, Any]) -> None:
    """Mark one step done from outside the operation's own thread; the last step done finishes it with ``result``."""
    with _lock:
        op = _read(ctx.places, op_id)
        op.steps[index] = {"text": op.steps[index]["text"], "state": DONE, "detail": detail}
        if all(s["state"] == DONE for s in op.steps):
            op.state, op.result = DONE, dict(result)
        _write(ctx.places, op)


def finish(ctx: Context, op_id: str, state: str, result: dict[str, Any] | None = None) -> None:
    with _lock:
        op = _read(ctx.places, op_id)
        op.state = state
        op.result = dict(result or {})
        _write(ctx.places, op)


def get(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    op_id = args.get("id")
    if not isinstance(op_id, str) or not _ID.match(op_id):
        raise Refused("op", "name an operation by the id its action returned")
    try:
        op = _read(ctx.places, op_id)
    except FileNotFoundError as exc:
        raise Refused("op", f"no operation {op_id}; start the action again") from exc
    return {"op": models.dump(op)}


def run(ctx: Context, op_id: str, steps: Sequence[Step], result: dict[str, Any]) -> str:
    """Carry out the steps in order, recording each; the first that fails ends the operation. Returns its state.

    A Refused step is the owner's to fix and is only recorded; any other failure is the factory's and is logged.
    """
    for index, (_, act) in enumerate(steps):
        step(ctx, op_id, index, RUNNING)
        try:
            detail = act()
        # telemetry: surfaced — the refusal is the failed `step`'s reason, which the dashboard draws
        except Refused as exc:
            step(ctx, op_id, index, FAILED, exc.reason)
        except StepError as exc:
            log.warning("warden: operation %s step %d failed (%s)", op_id, index, type(exc).__name__)
            step(ctx, op_id, index, FAILED, exc.detail)
        except Exception as exc:
            log.exception("warden: operation %s step %d failed (%s)", op_id, index, type(exc).__name__)
            step(ctx, op_id, index, FAILED, f"the warden failed ({type(exc).__name__})")
        else:
            step(ctx, op_id, index, DONE, detail)
            continue
        finish(ctx, op_id, FAILED, result)
        return FAILED
    finish(ctx, op_id, DONE, result)
    return DONE


def launch(ctx: Context, kind: str, steps: Sequence[Step], result: dict[str, Any] | None = None) -> str:
    """Start an operation and run its steps in the background; returns its id at once."""
    op_id = start(ctx, kind, [text for text, _ in steps])
    thread = threading.Thread(target=run, args=(ctx, op_id, steps, result if result is not None else {}),
                              name=f"op-{kind}", daemon=True)  # fmt: skip
    _threads[op_id] = thread
    thread.start()
    return op_id


def join(op_id: str, timeout: float | None = None) -> None:
    """Wait for an operation this process launched (the rehearsal and tests use it)."""
    thread = _threads.pop(op_id, None)
    if thread is not None:
        thread.join(timeout)


def recover(places: Places, clock: Clock) -> None:
    """Mark every operation a stopped warden left running as failed at the step it had reached."""
    for path in sorted((places.state / "ops").glob("op-*.json")):
        op = models.load(Operation, store.read_json(path))
        if op.state != RUNNING:
            continue
        for entry in op.steps:
            if entry["state"] == RUNNING:
                entry["state"], entry["detail"] = FAILED, f"the warden restarted during this step ({clock.iso()})"
        op.state = FAILED
        _write(places, op)
