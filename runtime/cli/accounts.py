"""`factory account`: the agents' Claude accounts, from the owner's own terminal.

    factory account list | add <name> | use <name> | remove <name>

These are owner verbs, so they go to owner.sock, the socket the dashboard asks. The warden knows the caller from the
socket's peer: it answers there only the owner's own uid and the dashboard's, and every session's request is refused
(the guard refuses `factory account` too). `add` reads the token from a hidden prompt, or from stdin when that is not
a terminal, and only ever sends it. `use` follows the switch to its end, printing each step.
"""

from __future__ import annotations

import argparse
import getpass
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

from runtime.cli.factory import UNREACHABLE, Call, show

# How often a switch's steps are read again while it runs, as the dashboard's operation sheet does.
_FOLLOW_SECONDS = 0.7
_MARKS = {"running": "...", "done": "done", "failed": "FAILED"}

Ask = Callable[[Call], dict[str, Any]]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="factory account", description="The agents' Claude accounts.")
    words = parser.add_subparsers(dest="cmd", required=True)
    words.add_parser("list", help="every account, the active one marked")
    for cmd, what in (("add", "add an account; its token is asked for, hidden"), ("use", "switch the agents to it"),
                      ("remove", "forget an account the agents do not use")):  # fmt: skip
        words.add_parser(cmd, help=what).add_argument("name")
    return parser


def read_token(stdin: IO[str], prompt: Callable[[str], str]) -> str:
    if stdin.isatty():
        return prompt("The token `claude setup-token` printed (hidden): ")
    return stdin.read()


def follow(ask: Ask, op_id: str, out: IO[str], err: IO[str], sleep: Callable[[float], None]) -> int:
    """Print each step of the operation as it moves, until it ends; 0 when it is done."""
    printed: dict[int, tuple[str, str]] = {}
    while True:
        answer = ask(Call("op.get", {"id": op_id}))
        if not answer.get("ok"):
            return show(answer, out, err)
        op = answer["op"]
        for index, step in enumerate(op["steps"]):
            seen = (step["state"], step["detail"])
            if step["state"] in _MARKS and printed.get(index) != seen:
                printed[index] = seen
                detail = f": {step['detail']}" if step["detail"] else ""
                print(f"  {_MARKS[step['state']]:<6} {step['text']}{detail}", file=out, flush=True)
        if op["state"] != "running":
            return 0 if op["state"] == "done" else 1
        sleep(_FOLLOW_SECONDS)


def _list(answer: dict[str, Any], out: IO[str]) -> None:
    for row in answer["accounts"]:
        if row.get("broken"):
            print(f"  {row['id']}: {row['error']}", file=out)
            continue
        mark = "active" if row["active"] else ""
        print(f"  {row['name']:<31} {mark:<6} …{row['last4']}  added {row['added']}", file=out)


def run(
    argv: list[str],
    ask: Ask,
    stdin: IO[str],
    out: IO[str],
    err: IO[str],
    prompt: Callable[[str], str] = getpass.getpass,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    ns = _parser().parse_args(argv)
    if ns.cmd == "list":
        answer = ask(Call("account.list", {}))
        if answer.get("ok"):
            _list(answer, out)
            return 0
        return show(answer, out, err)
    data = read_token(stdin, prompt).encode() if ns.cmd == "add" else None
    answer = ask(Call(f"account.{ns.cmd}", {"name": ns.name}, data))
    if not answer.get("ok") or "op" not in answer:
        return show({k: v for k, v in answer.items() if k in ("ok", "refused", "reason", "message")}, out, err)
    print(answer["message"], file=out, flush=True)
    return follow(ask, answer["op"], out, err, sleep)


def main(argv: list[str], owner_socket: Path, ask: Callable[[Path, Call], dict[str, Any]], stdin: IO[str],
         out: IO[str], err: IO[str]) -> int:  # fmt: skip
    try:
        return run(argv, lambda call: ask(owner_socket, call), stdin, out, err)
    # telemetry: surfaced — the owner reads on `err` that the warden cannot be reached, and the exit says so
    except (OSError, ValueError) as exc:
        print(f"factory: the warden cannot be reached ({type(exc).__name__}); is it running?", file=err)
        return UNREACHABLE
