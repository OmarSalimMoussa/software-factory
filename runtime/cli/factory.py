"""The `factory` command: every session and the owner's own terminal ask the warden through it.

It turns a command line into one request on the warden's socket and prints the answer. The warden knows who is
asking from the calling process, never from these arguments. Every file the caller names (a picture, a spec, a
body given with ``--body``) is read here, as the caller, and sent as bytes; the warden never opens a path a caller
names. Nothing comes from stdin: Claude Code runs a call outside the sandbox, where the warden's socket is
reachable, only when the call is ``factory …`` alone, with no pipe, redirect or heredoc. The guard puts
``--agent-id <id>`` first on a subagent's call. `check`, `qa-browser` and `slot-api` run here without the warden.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from runtime.shared import protocol
from runtime.shared.paths import CHECK_TOOLS, Places

LOCAL = ("check", "qa-browser", "slot-api")
# Run under the tools' Python in isolated mode: the install's root comes in as the first argument.
_RUN_CHECKS = (
    "import sys; sys.path.insert(0, sys.argv.pop(1)); from checks.runner.cli import main; sys.exit(main(sys.argv[1:]))"
)
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="factory", description="Ask the factory's warden.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    start = sub.add_parser("start", help="start an implementer or designer (orchestrator), or the orchestrator")
    start.add_argument("role", choices=["implementer", "designer", "orchestrator"])
    start.add_argument("target", nargs="?", default="")
    start.add_argument("--note", default="")
    start.add_argument("--outage", action="store_true")
    stop = sub.add_parser("stop")
    stop.add_argument("session")
    stop.add_argument("--why", default="")
    resume = sub.add_parser("resume")
    resume.add_argument("session")
    resume.add_argument("message")
    extend = sub.add_parser("extend")
    extend.add_argument("session")
    extend.add_argument("limit")
    bench = sub.add_parser("bench")
    bench.add_argument("session")
    bench.add_argument("--for", dest="for_task", required=True)
    sub.add_parser("revive").add_argument("session")
    sub.add_parser("doing").add_argument("text")
    sub.add_parser("tick")
    sub.add_parser("inbox").add_argument("--wait", action="store_true")
    sub.add_parser("push").add_argument("sha", nargs="?")
    sub.add_parser("promote").add_argument("--back", action="store_true")
    reflect = sub.add_parser("reflect")
    reflect.add_argument("--kind", required=True)
    reflect.add_argument("text")
    sub.add_parser("status")
    sub.add_parser("settings").add_argument("what", choices=["show"])
    _telemetry(sub)
    request = sub.add_parser("request", help="request new --from … --severity … --where … --body <file>")
    request.add_argument("what", choices=["new"])
    request.add_argument("--from", dest="sender", default="")
    request.add_argument("--severity", required=True)
    request.add_argument("--where", required=True)
    request.add_argument("--body", required=True)
    triage = sub.add_parser("triage", help="triage apply --body <plan.json>")
    triage.add_argument("what", choices=["apply"])
    triage.add_argument("--body", required=True)
    _queue(sub)
    sub.add_parser("route").add_argument("task")
    propose = sub.add_parser("propose")
    propose.add_argument("task", nargs="?", default="")
    propose.add_argument("--none", action="store_true")
    propose.add_argument("--picture")
    propose.add_argument("--why", default="")
    propose.add_argument("--run", type=int)
    propose.add_argument("--reason", default="")
    spec = sub.add_parser("spec")
    spec.add_argument("what", choices=["save"])
    spec.add_argument("task")
    spec.add_argument("file")
    card = sub.add_parser("card")
    card.add_argument("what", choices=["comments"])
    card.add_argument("task")
    card.add_argument("count", type=int)
    needs = sub.add_parser("needs-owner")
    needs.add_argument("task", nargs="?", default="")
    needs.add_argument("--picture", required=True)
    needs.add_argument("--brief", default="")
    needs.add_argument("--ask", default="")
    needs.add_argument("--question", default="")
    merge = sub.add_parser("merge")
    merge.add_argument("what", choices=["recorded"])
    merge.add_argument("branch")
    merge.add_argument("sha")
    _slot(sub)
    qa = sub.add_parser("qa")
    qa.add_argument("what", choices=["plan", "result"])
    qa.add_argument("--head", default="")
    for key in ("blockers", "majors", "minors"):
        qa.add_argument(f"--{key}", type=int)
    ready = sub.add_parser("qa-ready")
    ready.add_argument("head")
    ready.add_argument("--slices", nargs="+", required=True)
    sub.add_parser("qa-clean")
    sub.add_parser("ready-check")
    return parser


def _facts(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--title")
    parser.add_argument("--goal-item")
    parser.add_argument("--severity")
    parser.add_argument("--surfaces", nargs="+")
    parser.add_argument("--cause")
    parser.add_argument("--slices", type=int)
    parser.add_argument("--owner-class")
    parser.add_argument("--after", nargs="+")
    parser.add_argument("--branch")


def _queue(sub: Any) -> None:
    queue = sub.add_parser("queue", help="the task priority queue")
    words = queue.add_subparsers(dest="queue_cmd", required=True)
    words.add_parser("next")
    check = words.add_parser("check", help="queue check <merge sha> --body <report file>")
    check.add_argument("commit")
    check.add_argument("--body", required=True)
    task = words.add_parser("task").add_subparsers(dest="task_cmd", required=True)
    new = task.add_parser("new", help="the task's text in --body <file>")
    new.add_argument("--body", required=True)
    new.add_argument("--requests", nargs="+", required=True)
    _facts(new)
    facts = task.add_parser("facts")
    facts.add_argument("task")
    _facts(facts)
    status = task.add_parser("status")
    status.add_argument("task")
    status.add_argument("to")
    status.add_argument("--event", default="")
    status.add_argument("--note", default="")


def _telemetry(sub: Any) -> None:
    for verb, flag, what in (("errors", "--issue", "a GlitchTip issue"), ("traces", "--trace", "a Langfuse trace")):
        parser = sub.add_parser(verb, help=f"{verb} of prod, staging or a slot, or {what}; read-only")
        parser.add_argument("--where", nargs="+", default=["prod"], metavar="prod|staging|slot N")
        parser.add_argument(flag, dest="one")


def _slot(sub: Any) -> None:
    slot = sub.add_parser("slot", help="your stack slot")
    ops = slot.add_subparsers(dest="op", required=True)
    ops.add_parser("claim").add_argument("--note", default="")
    ops.add_parser("up").add_argument("services", nargs="*")
    for name in ("down", "release", "list", "reports"):
        ops.add_parser(name)
    ops.add_parser("free").add_argument("slot", type=int)
    ops.add_parser("sign-in").add_argument("email")
    seed = ops.add_parser("seed")
    seed.add_argument("seed", nargs="+")
    seed.add_argument("--as", dest="as_email", default="")
    ops.add_parser("desktop").add_argument("desktop", nargs="+")


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
    call = _call(_parser().parse_args(argv), read_file)
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
    if cmd in ("stop", "revive"):
        return Call(cmd, {"session": ns.session, **({"why": ns.why} if cmd == "stop" and ns.why else {})})
    if cmd == "resume":
        return Call("resume", {"session": ns.session, "message": ns.message})
    if cmd == "extend":
        return Call("extend", {"session": ns.session, "limit": ns.limit})
    if cmd == "bench":
        return Call("bench", {"session": ns.session, "for": ns.for_task})
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
        return Call("triage.apply", {}, read_file(ns.body))
    if cmd == "queue":
        return _queue_call(ns, read_file)
    if cmd == "route":
        return Call("route", {"task": ns.task})
    if cmd == "propose":
        return _propose_call(ns, read_file)
    if cmd == "spec":
        return Call("spec.save", {"task": ns.task}, read_file(ns.file))
    if cmd == "card":
        return Call("card.comments", {"task": ns.task, "count": ns.count})
    if cmd == "needs-owner":
        args = {k: getattr(ns, k) for k in ("task", "brief", "ask", "question") if getattr(ns, k)}
        return Call("needs-owner", args, read_file(ns.picture))
    if cmd == "merge":
        return Call("merge.recorded", {"branch": ns.branch, "sha": ns.sha})
    if cmd == "slot":
        return _slot_call(ns)
    if cmd == "qa":
        return _qa_call(ns)
    if cmd == "qa-ready":
        return Call("qa-ready", {"head": ns.head, "slices": _words(ns.slices)})
    raise CliError(f"factory {cmd} has no request")


def _queue_call(ns: argparse.Namespace, read_file: Callable[[str], bytes]) -> Call:
    if ns.queue_cmd == "next":
        return Call("queue.next", {})
    if ns.queue_cmd == "check":
        return Call("queue.check", {"commit": ns.commit}, read_file(ns.body))
    if ns.task_cmd == "new":
        return Call("task.new", {"requests": _words(ns.requests), **_fact_args(ns)}, read_file(ns.body))
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
        raise CliError("factory propose <task> --picture <file> --why '…' [--run N --reason '…']")
    args = {"task": ns.task, "why": ns.why, **({"run": ns.run} if ns.run is not None else {})}
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


def _qa_call(ns: argparse.Namespace) -> Call:
    if ns.what == "plan":
        return Call("qa.plan", {})
    counts = {k: getattr(ns, k) for k in ("blockers", "majors", "minors")}
    if not ns.head or any(v is None for v in counts.values()):
        raise CliError("factory qa result --head <sha> --blockers <n> --majors <n> --minors <n>")
    return Call("qa.result", {"head": ns.head, **counts})


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
    rest = {k: v for k, v in answer.items() if k != "ok"}
    print(
        rest["message"] if set(rest) <= {"message", "events"} and "message" in rest else json.dumps(rest, indent=1),
        file=out,
    )
    return 0


def _local(argv: list[str], places: Places) -> int:
    if argv[0] == "check":
        # The checks find ruff, mypy and lint-imports beside the Python they run under: the pinned tools' own.
        python = CHECK_TOOLS / "bin" / "python"
        if not python.is_file():
            raise CliError(f"the check tools are not installed at {CHECK_TOOLS}: reinstall the factory")
        root = str(Path(__file__).resolve().parents[2])
        os.execv(python, [str(python), "-I", "-c", _RUN_CHECKS, root, *argv[1:]])
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
