"""Every command the `factory` command takes: its words, its arguments and the help each one prints."""

from __future__ import annotations

import argparse
from typing import Any

from runtime.shared.models import CHAT_PAGES


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="factory", description="Ask the factory's warden.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    _sessions(sub)
    _telemetry(sub)
    _requests(sub)
    _queue(sub)
    _tasks(sub)
    _slot(sub)
    _qa(sub)
    return parser


def _sessions(sub: Any) -> None:
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
    sub.add_parser("doing").add_argument("text")
    sub.add_parser("tick")
    sub.add_parser("inbox").add_argument("--wait", action="store_true")
    decide = sub.add_parser("decide")
    decide.add_argument("card")
    decide.add_argument("choice")
    sub.add_parser("promote").add_argument("--back", action="store_true")
    reflect = sub.add_parser("reflect")
    reflect.add_argument("--kind", required=True)
    reflect.add_argument("text")
    sub.add_parser("status")
    sub.add_parser("settings").add_argument("what", choices=["show"])
    sub.add_parser("account", help="the agents' Claude accounts, the owner's own: list, add, use or remove")


def _requests(sub: Any) -> None:
    request = sub.add_parser("request", help="request new --from … --severity … --where … --body <file>")
    request.add_argument("what", choices=["new"])
    request.add_argument("--from", dest="sender", default="")
    request.add_argument("--severity", required=True)
    request.add_argument("--where", required=True)
    request.add_argument("--body", required=True)
    triage = sub.add_parser("triage", help="triage plan --body <plan.json> (the triage subagent), or triage apply")
    triage.add_argument("what", choices=["plan", "apply"])
    triage.add_argument("--body")


def _tasks(sub: Any) -> None:
    sub.add_parser("route").add_argument("task")
    propose = sub.add_parser("propose")
    propose.add_argument("task", nargs="?", default="")
    propose.add_argument("--none", action="store_true")
    propose.add_argument("--picture")
    propose.add_argument("--why", default="")
    propose.add_argument("--run", type=int)
    propose.add_argument("--reason", default="")
    propose.add_argument("--brief", help="a file holding the brief the run starts with once approved")
    spec = sub.add_parser("spec")
    spec.add_argument("what", choices=["save"])
    spec.add_argument("task")
    spec.add_argument("file")
    spec.add_argument("--slices", type=int, required=True, help="the number of slices the spec lists")
    brief = sub.add_parser("brief", help="your task's brief: save a version, read the owner's comments, answer one")
    brief.add_argument("what", choices=["save", "comments", "reply"])
    brief.add_argument("task")
    brief.add_argument("rest", nargs="*", help="save: the HTML file; reply: the comment's id, then your answer")
    needs = sub.add_parser("needs-owner")
    needs.add_argument("task", nargs="?", default="")
    needs.add_argument("--picture", required=True)
    needs.add_argument("--ask", default="")
    needs.add_argument("--question", default="")
    sub.add_parser("turn", help="give the merge turn to the next qa-clean task").add_argument("what", choices=["next"])
    cause = sub.add_parser("cause", help="cause --tried '<the idea, and what ruled it out>': one try that did not hold")
    cause.add_argument("--tried", required=True)


def _qa(sub: Any) -> None:
    said = ("qa plan | step --head … --step <item>:<n> | step-result --head … --step <item>:<n> --result "
            "pass|fail|blocked --got … | finding --head … --severity … --where … [--step <item>:<n>] --body <file> | "
            "result --head … | waive <task> <finding> --why …")  # fmt: skip
    qa = sub.add_parser("qa", help=said)
    qa.add_argument("what", choices=["plan", "step", "step-result", "finding", "result", "waive", "scenario"])
    qa.add_argument("words", nargs="*")
    qa.add_argument("--head", default="")
    qa.add_argument("--severity", default="")
    qa.add_argument("--where", default="")
    qa.add_argument("--step", default="")
    qa.add_argument("--result", default="")
    qa.add_argument("--got", default="")
    qa.add_argument("--body")
    qa.add_argument("--why", default="")
    ready = sub.add_parser("qa-ready")
    ready.add_argument("head")
    ready.add_argument("--slices", nargs="+", help="one commit per slice; by default each slice's Slice: n/N commit")
    ready.add_argument("--reproduce", action="store_true", help="before your first commit: testers reproduce a bug")
    sub.add_parser("qa-clean")
    sub.add_parser("ready-check")
    check = sub.add_parser("check", help="the standard checks on your checkout; the warden runs them")
    scope = check.add_mutually_exclusive_group()
    scope.add_argument("--all", action="store_true", help="check everything, not only what the branch adds")
    scope.add_argument("--list", action="store_true", help="name every check and its rule")
    check.add_argument("--only", metavar="IDS", help="run only these checks (comma-separated); records no pass")


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
    check = words.add_parser("check", help="queue check <the commit walked> --body <report file>")
    check.add_argument("commit")
    check.add_argument("--body", required=True)
    bundle = words.add_parser("bundle", help="the merges a check clears together, where to walk them, and what")
    bundle.add_argument("--through", default="", help="a commit past the cap, once the check of the cap failed")
    task = words.add_parser("task").add_subparsers(dest="task_cmd", required=True)
    new = task.add_parser("new", help="the task's text in --body <file>")
    new.add_argument("--body", required=True)
    new.add_argument("--requests", nargs="+", required=True)
    _facts(new)
    facts = task.add_parser("facts")
    facts.add_argument("task")
    _facts(facts)
    task.add_parser("show", help="the task, its log, its requests and its spec, from any worktree").add_argument("task")
    status = task.add_parser("status")
    status.add_argument("task")
    status.add_argument("to")
    status.add_argument("--event", default="")
    status.add_argument("--note", default="")
    status.add_argument("--after", default="", help="on-hold: the task this one waits for")


def _telemetry(sub: Any) -> None:
    errors = sub.add_parser(
        "errors", help="errors of prod or a slot (slot 0 is staging), or a GlitchTip issue; read-only"
    )
    errors.add_argument("--where", nargs="+", default=["prod"], metavar="prod|slot N")
    errors.add_argument("--issue")
    errors.add_argument("--whole", action="store_true", help="with --issue: the newest event, all of it")
    logs = sub.add_parser("logs", help="log lines of prod or a slot (slot 0 is staging), newest first; read-only")
    logs.add_argument("--where", nargs="+", default=["prod"], metavar="prod|slot N")
    logs.add_argument("--find", nargs="+", help="only lines whose text holds these words, in this order")
    logs.add_argument("--since", help="only lines from this time on, such as 2026-10-17T09:17:00Z")
    chat = sub.add_parser("chat", help="the talk around a request from the chat, a page at a time; read-only")
    chat.add_argument("request")
    place = chat.add_mutually_exclusive_group()
    for position in CHAT_PAGES:
        place.add_argument(f"--{position}", metavar="<message id>", help=f"the page {position} that message")


def _slot(sub: Any) -> None:
    slot = sub.add_parser("slot", help="your stack slot")
    ops = slot.add_subparsers(dest="op", required=True)
    ops.add_parser("claim").add_argument("--note", default="")
    up = "build and start the stack, or the named services; a running service whose image is unchanged keeps running"
    slot_up = ops.add_parser("up", help=up)
    slot_up.add_argument("services", nargs="*")
    slot_up.add_argument("--at", default="", help="slot 0 only: a commit of the default branch older than its tip")
    ops.add_parser("restart", help="restart the named services, running ones").add_argument("services", nargs="+")
    ops.add_parser("stop", help="stop the named services, keeping their data").add_argument("services", nargs="+")
    for name in ("down", "release", "list"):
        ops.add_parser(name)
    ops.add_parser("top", help="the CPU of each container running for your slot, and of each process in it")
    ops.add_parser("free").add_argument("slot", type=int)
    ops.add_parser("sign-in").add_argument("email")
    seed = ops.add_parser("seed")
    seed.add_argument("seed", nargs="+")
    seed.add_argument("--as", dest="as_email", default="")
    ops.add_parser("desktop").add_argument("desktop", nargs="+")
    run = ops.add_parser("run", usage="factory slot run <service> -- <command> [word ...]")
    run.add_argument("service")
    run.add_argument("command", nargs="+")
