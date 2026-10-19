"""A tester's steps: where each item's numbered step started, and what the tester saw at its end. The replay splits
the run at these marks and words each step from them: its name and what it expects from the scenario file, what the
tester got from its result.

`factory qa step --step <item>:<n>` marks a step's start; `factory qa step-result --step <item>:<n> --result
pass|fail|blocked --got '…'` its end. A result for a step the tester never started is refused, so every result has a
start, and so is a start while a step of the same item has no result, so an item's steps never overlap in the replay;
a step of another item, such as a hold the brief counts as its own, may run inside one. An implementer's session keeps
its testers' marks in the warden's state.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from runtime.qa import scenarios
from runtime.queue import formats
from runtime.shared import store
from runtime.shared.protocol import Refused
from runtime.warden.context import Context

# An item is a scenario's file name without .md, or a short name for a check of the brief: words joined by dashes.
ITEM = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
RESULTS = ("pass", "fail", "blocked")


def path(ctx: Context, session_id: str) -> Path:
    return ctx.places.steps_of(session_id)


def _mark(ctx: Context, args: dict[str, Any], verb: str) -> tuple[str, int]:
    item, step = args.get("item"), args.get("step")
    if not isinstance(item, str) or not ITEM.match(item):
        raise Refused(verb, "name the item as your brief does: a scenario's file name or a check's short name")
    settings = ctx.settings()
    most = scenarios.most_steps(settings.get_int("qa.max_items"), settings.get_int("qa.max_steps"))
    if not isinstance(step, int) or isinstance(step, bool) or not 1 <= step <= most:
        raise Refused(verb, f"the step is its number in the item, from 1 to {most}")
    return item, step


def start(ctx: Context, session_id: str, head: str, tester: str, agent: str, args: dict[str, Any]) -> dict[str, Any]:
    """Mark the start of one step of ``agent``'s item on ``head``, once its item's earlier steps have their end."""
    item, step = _mark(ctx, args, "qa-step")
    record = {"item": item, "step": step, "head": head, "tester": tester, "agent": agent, "at": ctx.clock.iso()}
    with ctx.lock:
        unended = [s for s in _unended(of_agent(ctx, session_id, agent)) if s[0] == item]
        if unended:
            was_item, was_step = unended[0]
            raise Refused(
                "qa-step",
                f"mark how step {was_step} of {was_item} ended first: factory qa step-result --head <sha> --step "
                f"{was_item}:{was_step} --result pass|fail|blocked --got '…'",
            )
        store.append_jsonl(path(ctx, session_id), record)
    return record


def _unended(marks: list[dict[str, Any]]) -> list[tuple[Any, Any]]:
    """The steps among ``marks`` started and not yet ended, in the order they started."""
    started: list[tuple[Any, Any]] = []
    for mark in marks:
        step = (mark.get("item"), mark.get("step"))
        started = [s for s in started if s != step] if "result" in mark else [*started, step]
    return started


def end(ctx: Context, session_id: str, head: str, tester: str, agent: str, args: dict[str, Any]) -> dict[str, Any]:
    """Record what ``agent`` saw at the end of a step it started."""
    item, step = _mark(ctx, args, "qa-step-result")
    result, got = args.get("result"), args.get("got")
    if result not in RESULTS:
        raise Refused("qa-step-result", f"the result is one of {', '.join(RESULTS)}")
    if not isinstance(got, str) or not got.strip():
        raise Refused("qa-step-result", "say in a few words what you saw: --got '…'")
    with ctx.lock:
        started = any(
            r.get("agent") == agent and r.get("item") == item and r.get("step") == step and "result" not in r
            for r in store.read_jsonl(path(ctx, session_id))
        )
        if not started:
            raise Refused("qa-step-result", f"start step {step} of {item} first: factory qa step --step {item}:{step}")
        record = {
            "item": item,
            "step": step,
            "result": result,
            "got": formats.one_line(got),
            "head": head,
            "tester": tester,
            "agent": agent,
            "at": ctx.clock.iso(),
        }
        store.append_jsonl(path(ctx, session_id), record)
    return record


def of_agent(ctx: Context, session_id: str, agent: str) -> list[dict[str, Any]]:
    """Every start and result one tester recorded, in the order it recorded them."""
    return [r for r in store.read_jsonl(path(ctx, session_id)) if r.get("agent") == agent]
