"""The `factory` command: every session and the owner's own terminal ask the warden through it.

It turns a command line into one request on the warden's socket and prints the answer. The warden knows who is
asking from the calling process, never from these arguments. Every file the caller names (a picture, a spec, a
body given with ``--body``) is read here, as the caller, and sent as bytes; the warden never opens a path a caller
names. Nothing comes from stdin: Claude Code runs a call outside the sandbox, where the warden's socket is
reachable, only when the call is ``factory …`` alone, with no pipe, redirect or heredoc. The guard puts
``--agent-id <id>`` first on a subagent's call. `qa-browser` and `slot-api` run here without the warden; `check` is
the warden's, which runs it walled into the caller's checkout; `account` is the owner's alone, on owner.sock
(runtime/cli/accounts.py).
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from runtime.cli import commands
from runtime.shared import protocol
from runtime.shared.paths import Places

LOCAL = ("qa-browser", "slot-api")
UNREACHABLE = 3


class CliError(Exception):
    """The command line cannot become a request; the message says what to type."""


@dataclass(frozen=True, slots=True)
class Call:
    verb: str
    args: dict[str, Any]
    data: bytes | None = None
    agent_id: str = ""


def _words(values: list[str] | None) -> list[str]:
    """Words given as `a b`, `a,b` or `a, b`."""
    return [w for value in values or [] for w in value.replace(",", " ").split()]


def _fact_args(ns: argparse.Namespace) -> dict[str, Any]:
    names = {
        "title": "title",
        "goal_item": "goal_item",
        "severity": "severity",
        "cause": "cause",
        "slices": "slices",
        "owner_class": "owner_class",
        "branch": "branch",
    }
    found: dict[str, Any] = {key: getattr(ns, attr) for key, attr in names.items() if getattr(ns, attr) is not None}
    for key in ("surfaces", "after"):
        if getattr(ns, key) is not None:
            found[key] = _words(getattr(ns, key))
    return found


def global_options(argv: list[str]) -> tuple[str, list[str]]:
    """The subagent id the guard put first on the command line, and the rest of the line."""
    if argv[:1] == ["--agent-id"]:
        if len(argv) < 2 or not argv[1] or argv[1].startswith("-"):
            raise CliError("--agent-id names the subagent")
        return argv[1], argv[2:]
    if argv[:1] and argv[0].startswith("--agent-id="):
        return argv[0].partition("=")[2], argv[1:]
    return "", argv


def build(argv: list[str], read_file: Callable[[str], bytes]) -> Call:
    """The request a command line makes; every file it names is read here, by the caller, and sent as bytes."""
    agent_id, argv = global_options(argv)
    call = _call(commands.parser().parse_args(argv), read_file)
    return Call(call.verb, call.args, call.data, agent_id)


def _call(ns: argparse.Namespace, read_file: Callable[[str], bytes]) -> Call:
    cmd = ns.cmd
    simple = {"tick": "tick", "status": "status", "qa-clean": "qa-clean", "ready-check": "ready-check"}
    if cmd in simple:
        return Call(simple[cmd], {})
    if cmd == "start":
        if ns.role == "orchestrator":
            return Call("start", {"role": ns.role})
        return Call("start", {"role": ns.role, "task": ns.target, "note": ns.note, "outage": ns.outage})
    if cmd == "stop":
        return Call(cmd, {"session": ns.session, **({"why": ns.why} if ns.why else {})})
    if cmd == "resume":
        return Call("resume", {"session": ns.session, "message": ns.message})
    if cmd == "extend":
        return Call("extend", {"session": ns.session, "limit": ns.limit})
    if cmd == "doing":
        return Call("doing", {"text": ns.text})
    if cmd == "inbox":
        return Call("inbox.wait" if ns.wait else "inbox", {})
    if cmd == "push":
        return Call("push", {"sha": ns.sha} if ns.sha else {})
    if cmd == "promote":
        return Call("promote", {"back": ns.back})
    if cmd == "reflect":
        return Call("reflect", {"kind": ns.kind, "text": ns.text})
    if cmd == "settings":
        return Call("settings.show", {})
    if cmd in ("errors", "traces"):
        return _telemetry_call(ns)
    if cmd == "request":
        args = {"severity": ns.severity, "where": ns.where, **({"from": ns.sender} if ns.sender else {})}
        return Call("request.new", args, read_file(ns.body))
    if cmd == "triage":
        if ns.what == "plan" and not ns.body:
            raise CliError("factory triage plan --body <plan.json>")
        return Call(f"triage.{ns.what}", {}, read_file(ns.body) if ns.body else None)
    if cmd == "queue":
        return _queue_call(ns, read_file)
    if cmd == "route":
        return Call("route", {"task": ns.task})
    if cmd == "propose":
        return _propose_call(ns, read_file)
    if cmd == "spec":
        return Call("spec.save", {"task": ns.task, "slices": ns.slices}, read_file(ns.file))
    if cmd == "card":
        return Call("card.comments", {"task": ns.task, "count": ns.count})
    if cmd == "needs-owner":
        args = {k: getattr(ns, k) for k in ("task", "brief", "ask", "question") if getattr(ns, k)}
        return Call("needs-owner", args, read_file(ns.picture))
    if cmd == "turn":
        return Call("turn.next", {})
    if cmd == "cause":
        return Call("cause", {"tried": ns.tried})
    if cmd == "slot":
        return _slot_call(ns)
    if cmd == "qa":
        return _qa_call(ns, read_file)
    if cmd == "qa-ready":
        return Call("qa-ready", {"head": ns.head, **({"slices": _words(ns.slices)} if ns.slices else {})})
    if cmd == "check":
        if ns.list:
            return Call("check", {"list": True})
        return Call("check", {"all": ns.all, **({"only": _words([ns.only])} if ns.only else {})})
    raise CliError(f"factory {cmd} has no request")


def _queue_call(ns: argparse.Namespace, read_file: Callable[[str], bytes]) -> Call:
    if ns.queue_cmd == "next":
        return Call("queue.next", {})
    if ns.queue_cmd == "check":
        return Call("queue.check", {"commit": ns.commit}, read_file(ns.body))
    if ns.task_cmd == "new":
        return Call("task.new", {"requests": _words(ns.requests), **_fact_args(ns)}, read_file(ns.body))
    if ns.task_cmd == "show":
        return Call("task.show", {"task": ns.task})
    if ns.task_cmd == "facts":
        return Call("task.facts", {"task": ns.task, **_fact_args(ns)})
    args = {"task": ns.task, "to": ns.to, **({"event": ns.event} if ns.event else {})}
    return Call("task.status", {**args, **({"note": ns.note} if ns.note else {})})


def _propose_call(ns: argparse.Namespace, read_file: Callable[[str], bytes]) -> Call:
    if ns.none:
        if ns.task or ns.picture:
            raise CliError("propose --none names no task and no picture")
        return Call("propose.none", {})
    if not ns.task or not ns.picture:
        raise CliError("factory propose <task> --picture <file> --why '…' [--brief <file>] [--run N --reason '…']")
    args = {"task": ns.task, "why": ns.why, **({"run": ns.run} if ns.run is not None else {})}
    if ns.brief:
        args["brief"] = read_file(ns.brief).decode("utf-8", errors="replace")
    return Call("propose", {**args, **({"reason": ns.reason} if ns.reason else {})}, read_file(ns.picture))


def _telemetry_call(ns: argparse.Namespace) -> Call:
    where = _words(ns.where)
    if where in (["prod"], ["staging"]):
        args: dict[str, Any] = {"where": where[0]}
    elif len(where) == 2 and where[0] == "slot" and where[1].isdigit():
        args = {"where": "slot", "slot": int(where[1])}
    else:
        raise CliError("--where is prod, staging or slot <n>")
    if ns.one is not None:
        args["issue" if ns.cmd == "errors" else "trace"] = ns.one
    return Call(ns.cmd, args)


def _slot_call(ns: argparse.Namespace) -> Call:
    args: dict[str, Any] = {"op": ns.op}
    if ns.op == "claim" and ns.note:
        args["note"] = ns.note
    elif ns.op == "up":
        args["services"] = ns.services
    elif ns.op == "free":
        args["slot"] = ns.slot
    elif ns.op == "sign-in":
        args["email"] = ns.email
    elif ns.op == "seed":
        args.update({"seed": ns.seed, **({"as": ns.as_email} if ns.as_email else {})})
    elif ns.op == "desktop":
        args["desktop"] = ns.desktop
    return Call("slot", args)


def _qa_call(ns: argparse.Namespace, read_file: Callable[[str], bytes]) -> Call:
    if ns.what == "plan":
        return Call("qa.plan", {})
    if ns.what == "waive":
        if len(ns.words) != 2 or not ns.why:
            raise CliError("factory qa waive <task> <finding> --why '…'")
        return Call("qa.waive", {"task": ns.words[0], "finding": ns.words[1], "why": ns.why})
    if not ns.head:
        raise CliError(f"factory qa {ns.what} --head <sha> …")
    if ns.what == "result":
        return Call("qa.result", {"head": ns.head})
    if not (ns.severity and ns.where and ns.body):
        raise CliError("factory qa finding --head <sha> --severity blocker|major|minor --where '…' --body <file>")
    args = {"head": ns.head, "severity": ns.severity, "where": ns.where}
    return Call("qa.finding", args, read_file(ns.body))


def read_file(path: str) -> bytes:
    return Path(path).read_bytes()


def ask(path: Path, call: Call) -> dict[str, Any]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(str(path))
        sock.sendall(protocol.request(call.verb, call.args, call.data, agent_id=call.agent_id))
        reply = b""
        while not reply.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            reply += chunk
    answer = json.loads(reply)
    if not isinstance(answer, dict):
        raise ValueError("the warden's answer is not an object")
    return answer


def show(answer: dict[str, Any], out: IO[str], err: IO[str]) -> int:
    if not answer.get("ok"):
        print(f"factory: refused ({answer.get('refused')}): {answer.get('reason')}", file=err)
        return 1
    if "output" in answer and "exit" in answer:
        # A check's report, as the check runner printed it, and its exit status.
        print(answer["output"], end="", file=out)
        return int(answer["exit"])
    if "checks" in answer:
        print("".join(f"{c['id']:20s} {c['rule']}\n" for c in answer["checks"]), end="", file=out)
        return 0
    rest = {k: v for k, v in answer.items() if k != "ok"}
    print(
        rest["message"] if set(rest) <= {"message", "events"} and "message" in rest else json.dumps(rest, indent=1),
        file=out,
    )
    return 0


def _local(argv: list[str], places: Places) -> int:
    if argv[0] == "qa-browser":
        from runtime.qa import qa_browser

        return qa_browser.main(argv[1:], places)
    from runtime.slots import slot_api

    return slot_api.main(lambda: ask(places.socket, Call("slot", {"op": "which"})))


def main(argv: list[str], places: Places | None = None) -> int:
    places = places or Places.system()
    try:
        rest = global_options(argv)[1]
        if rest[:1] and rest[0] in LOCAL:
            return _local(rest, places)
        if rest[:1] == ["account"]:
            from runtime.cli import accounts

            return accounts.main(rest[1:], places.owner_socket, ask, sys.stdin, sys.stdout, sys.stderr)
        call = build(argv, read_file)
    except (CliError, OSError) as exc:
        print(f"factory: {exc}", file=sys.stderr)
        return 2
    try:
        answer = ask(places.socket, call)
        while call.verb == "inbox.wait" and answer.get("ok") and not answer.get("events"):
            answer = ask(places.socket, call)  # the warden answers after watch.inbox_wait_seconds of quiet
    except (OSError, ValueError) as exc:
        print(f"factory: the warden cannot be reached ({type(exc).__name__}); is it running?", file=sys.stderr)
        return UNREACHABLE
    return show(answer, sys.stdout, sys.stderr)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
