"""The `factory` command: every session and the owner's own terminal ask the warden through it.

It turns a command line into one request on the warden's socket and prints the answer. The warden knows who is
asking from the calling process, never from these arguments. Every file the caller names (a picture, a spec, a
body given with ``--body``) is read here, as the caller, and sent as bytes; the warden never opens a path a caller
names. Nothing comes from stdin: the guard lets a call through only when it is ``factory …`` alone, with no pipe,
redirect or heredoc, so it can put ``--agent-id <id>`` first on a subagent's call. `qa-browser` and `slot-api` run
here without the warden; `check` is the warden's, which runs it walled into the caller's checkout; `account` is the
owner's alone, on owner.sock (runtime/cli/accounts.py).
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
from runtime.shared import lanes, protocol
from runtime.shared.models import CHAT_PAGES, SEVERITIES
from runtime.shared.paths import Places

LOCAL = (lanes.BROWSER, "slot-api")
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
    make = _CALLS.get(ns.cmd)
    if make is None:
        raise CliError(f"factory {ns.cmd} has no request")
    return make(ns, read_file)


def _start_call(ns: argparse.Namespace, _: Callable[[str], bytes]) -> Call:
    if ns.role == "orchestrator":
        return Call("start", {"role": ns.role})
    return Call("start", {"role": ns.role, "task": ns.target, "note": ns.note, "outage": ns.outage})


def _request_call(ns: argparse.Namespace, read_file: Callable[[str], bytes]) -> Call:
    args = {"severity": ns.severity, "where": ns.where, **({"from": ns.sender} if ns.sender else {})}
    return Call("request.new", args, read_file(ns.body))


def _triage_call(ns: argparse.Namespace, read_file: Callable[[str], bytes]) -> Call:
    if ns.what == "plan" and not ns.body:
        raise CliError("factory triage plan --body <plan.json>")
    return Call(f"triage.{ns.what}", {}, read_file(ns.body) if ns.body else None)


def _needs_owner_call(ns: argparse.Namespace, read_file: Callable[[str], bytes]) -> Call:
    args = {k: getattr(ns, k) for k in ("task", "ask", "question") if getattr(ns, k)}
    return Call("needs-owner", args, read_file(ns.picture))


def _brief_call(ns: argparse.Namespace, read_file: Callable[[str], bytes]) -> Call:
    if ns.what == "save":
        if len(ns.rest) != 1:
            raise CliError("factory brief save <task> <file.html>")
        return Call("brief.save", {"task": ns.task}, read_file(ns.rest[0]))
    if ns.what == "comments":
        return Call("brief.comments", {"task": ns.task})
    if len(ns.rest) != 2:
        raise CliError('factory brief reply <task> <comment> "<your answer>"')
    return Call("brief.reply", {"task": ns.task, "thread": ns.rest[0], "text": ns.rest[1]})


def _check_call(ns: argparse.Namespace, _: Callable[[str], bytes]) -> Call:
    if ns.list:
        return Call("check", {"list": True})
    return Call("check", {"all": ns.all, **({"only": _words([ns.only])} if ns.only else {})})


def _queue_call(ns: argparse.Namespace, read_file: Callable[[str], bytes]) -> Call:
    if ns.queue_cmd in ("next", "check", "bundle"):
        return _default_branch_call(ns, read_file)
    if ns.task_cmd == "new":
        return Call("task.new", {"requests": _words(ns.requests), **_fact_args(ns)}, read_file(ns.body))
    if ns.task_cmd == "show":
        return Call("task.show", {"task": ns.task})
    if ns.task_cmd == "facts":
        return Call("task.facts", {"task": ns.task, **_fact_args(ns)})
    args = {"task": ns.task, "to": ns.to, **({"event": ns.event} if ns.event else {})}
    told = {**({"note": ns.note} if ns.note else {}), **({"after": ns.after} if ns.after else {})}
    return Call("task.status", {**args, **told})


def _default_branch_call(ns: argparse.Namespace, read_file: Callable[[str], bytes]) -> Call:
    """`queue next`, and a check of the default branch: its bundle, and the check saved."""
    if ns.queue_cmd == "next":
        return Call("queue.next", {})
    if ns.queue_cmd == "check":
        return Call("queue.check", {"commit": ns.commit}, read_file(ns.body) if ns.body else None)
    return Call("queue.bundle", {"through": ns.through} if ns.through else {})


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
    if where == ["prod"]:
        args: dict[str, Any] = {"where": "prod"}
    elif len(where) == 2 and where[0] == "slot" and where[1].isdigit():
        args = {"where": "slot", "slot": int(where[1])}
    else:
        raise CliError("--where is prod or slot <n>; staging is slot 0")
    given = {"find": None if ns.find is None else " ".join(ns.find), "since": ns.since}
    if ns.cmd == "errors":
        given |= {"issue": ns.issue, "whole": ns.whole or None}
    args.update({key: value for key, value in given.items() if value is not None})
    return Call(ns.cmd, args)


def _slot_call(ns: argparse.Namespace) -> Call:
    args: dict[str, Any] = {"op": ns.op}
    if ns.op == "claim" and ns.note:
        args["note"] = ns.note
    elif ns.op == "up":
        args.update({"services": ns.services, **({"at": ns.at} if ns.at else {})})
    elif ns.op in ("restart", "stop"):
        args["services"] = ns.services
    elif ns.op == "free":
        args["slot"] = ns.slot
    elif ns.op == "sign-in":
        args["email"] = ns.email
    elif ns.op == "seed":
        args.update({"seed": ns.seed, **({"as": ns.as_email} if ns.as_email else {})})
    elif ns.op == "desktop":
        args["desktop"] = ns.desktop
    elif ns.op == "run":
        args.update({"service": ns.service, "command": ns.command})
    elif ns.op == "exec":
        args.update({"container": ns.container, "command": ns.command})
    return Call("slot", args)


def _qa_call(ns: argparse.Namespace, read_file: Callable[[str], bytes]) -> Call:
    if ns.what == "plan":
        return Call("qa.plan", {})
    if ns.what == "waive":
        if len(ns.words) != 2 or not ns.why:
            raise CliError("factory qa waive <task> <finding> --why '…'")
        return Call("qa.waive", {"task": ns.words[0], "finding": ns.words[1], "why": ns.why})
    if ns.what == "scenario":
        if len(ns.words) != 1:
            raise CliError("factory qa scenario <name>")
        return Call("qa.scenario", {"name": ns.words[0]})
    if not ns.head:
        raise CliError(f"factory qa {ns.what} --head <sha> …")
    if ns.what == "result":
        return Call("qa.result", {"head": ns.head, **({"blocked": ns.blocked} if ns.blocked else {})})
    if ns.what in ("step", "step-result"):
        return _step_call(ns)
    if not (ns.severity and ns.where and ns.body):
        raise CliError(f"factory qa finding --head <sha> --severity {'|'.join(SEVERITIES)} --where '…' "
                       "[--step <item>:<n>] --body <file>")  # fmt: skip
    args = {"head": ns.head, "severity": ns.severity, "where": ns.where, **({"step": ns.step} if ns.step else {})}
    return Call("qa.finding", args, read_file(ns.body))


def _step_call(ns: argparse.Namespace) -> Call:
    """`qa step --step <item>:<n>` and `qa step-result --step <item>:<n> --result <result> --got '…'`, the step named
    as a finding names it. Options only: Python 3.12's argparse drops words that follow an option here."""
    result = ns.what == "step-result"
    item, _, number = ns.step.partition(":")
    if ns.words or not (item and number.isdigit()) or (result and not (ns.result and ns.got)):
        usage = " --result pass|fail|blocked --got '<what you saw>'" if result else ""
        raise CliError(f"factory qa {ns.what} --head <sha> --step <item>:<n>{usage}")
    args: dict[str, Any] = {"head": ns.head, "item": item, "step": int(number)}
    if result:
        args |= {"result": ns.result, "got": ns.got}
    return Call("qa.step-result" if result else "qa.step", args)


_CALLS: dict[str, Callable[[argparse.Namespace, Callable[[str], bytes]], Call]] = {
    **{cmd: lambda ns, _: Call(ns.cmd, {}) for cmd in ("tick", "status", "qa-clean", "ready-check")},
    "start": _start_call,
    "stop": lambda ns, _: Call("stop", {"session": ns.session, **({"why": ns.why} if ns.why else {})}),
    "resume": lambda ns, _: Call("resume", {"session": ns.session, "message": ns.message}),
    "extend": lambda ns, _: Call("extend", {"session": ns.session, "limit": ns.limit}),
    "doing": lambda ns, _: Call("doing", {"text": ns.text}),
    "inbox": lambda ns, _: Call("inbox.wait" if ns.wait else "inbox", {}),
    "decide": lambda ns, _: Call("decide", {"card": ns.card, "choice": ns.choice}),
    "promote": lambda ns, _: Call("promote", {"back": ns.back}),
    "reflect": lambda ns, _: Call("reflect", {"kind": ns.kind, "text": ns.text}),
    "settings": lambda ns, _: Call("settings.show", {}),
    "errors": lambda ns, _: _telemetry_call(ns),
    "logs": lambda ns, _: _telemetry_call(ns),
    "records": lambda ns, _: Call("records", {"path": ns.path} if ns.path else {}),
    "chat": lambda ns, _: Call(
        "chat", {"request": ns.request, **{p: getattr(ns, p) for p in CHAT_PAGES if getattr(ns, p)}}
    ),
    "request": _request_call,
    "triage": _triage_call,
    "queue": _queue_call,
    "route": lambda ns, _: Call("route", {"task": ns.task}),
    "propose": _propose_call,
    "spec": lambda ns, read: Call("spec.save", {"task": ns.task, "slices": ns.slices}, read(ns.file)),
    "brief": _brief_call,
    "needs-owner": _needs_owner_call,
    "turn": lambda ns, _: Call("turn.next", {}),
    "cause": lambda ns, _: Call("cause", {"tried": ns.tried}),
    "slot": lambda ns, _: _slot_call(ns),
    "qa": _qa_call,
    "qa-ready": lambda ns, _: Call(
        "qa-ready",
        {
            "head": ns.head,
            **({"slices": _words(ns.slices)} if ns.slices else {}),
            **({"reproduce": True} if ns.reproduce else {}),
        },
    ),
    "check": _check_call,
}


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
    if argv[0] == lanes.BROWSER:
        from runtime.qa import qa_browser

        def sign_in(address: str) -> dict[str, Any]:
            return ask(places.socket, Call("slot", {"op": "sign-in", "email": address}))

        return qa_browser.main(argv[1:], places, sign_in)
    from runtime.slots import slot_api

    return slot_api.main(lambda: ask(places.socket, Call("slot", {"op": "which"})))


def _read(path: str) -> bytes:
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        # A file's name is shown whole, since its end tells it apart; text given in its place is cut short.
        shown = path if not any(c.isspace() for c in path) else path[:60]
        raise CliError(f"{shown!r} is not a file this can read ({exc.strerror}): write the text to a file "
                       "first, then name the file") from exc  # fmt: skip


def main(argv: list[str], places: Places | None = None) -> int:
    places = places or Places.system()
    try:
        rest = global_options(argv)[1]
        if rest[:1] and rest[0] in LOCAL:
            return _local(rest, places)
        if rest[:1] == ["account"]:
            from runtime.cli import accounts

            return accounts.main(rest[1:], places.owner_socket, ask, sys.stdin, sys.stdout, sys.stderr)
        call = build(argv, _read)
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
    folder = answer.get("folder") if call.verb in ("ready-check", "qa.scenario") and answer.get("ok") else None
    if folder:
        # A tester's own folder is there before its first file: `curl -c` into a missing folder saves nothing.
        try:
            Path(str(folder)).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"factory: your folder {folder} cannot be made ({exc.strerror})", file=sys.stderr)
            return 2
    return show(answer, sys.stdout, sys.stderr)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
