"""What a template unit's runner reads: one request the warden wrote to public/launch/<id>.json, checked before
anything runs, and the effective settings and sessions the warden published beside it.

The warden is the only writer of public/, so these checks are a second wall: a command of any shape but the few the
warden runs, a path outside the agent's home or the product checkout, a flag where a value belongs, sandbox settings
the warden did not write, or a slot that does not exist is refused, and the runner exits without running anything.
"""

from __future__ import annotations

import json
import os
import pty
import re
import select
import subprocess
import time
from pathlib import Path
from typing import Any

from runtime.shared.paths import Places
from runtime.shared.settings import Settings
from runtime.shared.settings_schema import SCHEMA
from runtime.slots import leases
from runtime.system import layout
from runtime.warden.sessions import launch, session_settings

SESSION, COMMAND, STACK, MESSAGE, CHECK = "session", "command", "stack", "message", "check"
REFUSED = 2
TIMED_OUT = 124
TOKEN_FD = "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR"  # where Claude Code finds the descriptor of its sign-in token
_ID = re.compile(r"^[A-Za-z0-9_-]{1,96}$")
PROGRAMS = {"claude": str(layout.CLAUDE_BIN), "git": "/usr/bin/git", "rm": "/usr/bin/rm", "kill": "/usr/bin/kill"}
# What a check request may pass the check runner: its flags, and check ids or a commit after them.
CHECK_FLAGS = frozenset({"--all", "--only", "--since"})
_CHECK_WORD = re.compile(r"^[a-z0-9][a-z0-9,-]{0,511}$")
# The verbs of the product's scripts/dev (factory spec, "scripts/dev"; plan R24 for reports).
# The product's tests run in the check unit (`factory check`), never on a slot.
DEV_VERBS = frozenset({"up", "down", "empty", "health", "sign-in", "seed", "desktop", "reports", "verbs", leases.RUN})
SWEEP, PRUNE, DISK, EVENTS = leases.SWEEP, leases.PRUNE, leases.DISK, leases.EVENTS
OWN_VERBS = frozenset({SWEEP, PRUNE, DISK, EVENTS})
_INSTALL = Path(__file__).resolve().parents[3]  # the runner runs from the install it belongs to
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SESSION_REF = re.compile(r"^[0-9a-f][0-9a-f-]{5,35}$")  # a short id or a uuid, as `claude stop`, `rm`, `respawn` take


class BadRequest(Exception):
    """The request cannot be run as written; the message says which part."""


def read(state: Path, request_id: str, kinds: tuple[str, ...]) -> dict[str, Any]:
    if not _ID.match(request_id):
        raise BadRequest(f"{request_id!r} is not a request id")
    path = layout.public(state) / "launch" / f"{request_id}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BadRequest(f"{path} cannot be read ({type(exc).__name__})") from exc
    if not isinstance(raw, dict) or raw.get("kind") not in kinds:
        raise BadRequest(f"{path} is not a {' or '.join(kinds)} request")
    return raw


def published(state: Path) -> tuple[Settings, dict[str, Any]]:
    """The effective settings and the sessions, as the warden last published them."""
    folder = layout.public(state)
    try:
        tree = json.loads((folder / "settings.json").read_text(encoding="utf-8"))
        sessions = json.loads((folder / "sessions.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BadRequest(f"the warden's public settings or sessions cannot be read ({type(exc).__name__})") from exc
    if not isinstance(tree, dict) or not isinstance(sessions, dict):
        raise BadRequest("the warden's public settings or sessions are not objects")
    return Settings(tree, tree, {}), sessions


def _value(raw: dict[str, Any], key: str, *, empty: bool = False) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or (not value and not empty) or value.startswith("-") or "\0" in value:
        raise BadRequest(f"{key} must be a word that is not a flag")
    return value


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _carried_ok(values: dict[str, Any], state: Path) -> None:
    """A session's options: plain values, the sandbox settings the warden wrote for it, and agent definitions as one
    JSON object."""
    for _, key in launch.CARRIED:
        _value(values, key)
    allowed = layout.public(state) / "session-settings"
    if not _inside(Path(os.path.normpath(values["settings"])), allowed) or not values["settings"].endswith(".json"):
        raise BadRequest(f"settings must be a file the warden wrote in {allowed}")
    try:
        agents = json.loads(values["agents"])
    except ValueError as exc:
        raise BadRequest("agents must be the agent definitions as JSON") from exc
    if not isinstance(agents, dict):
        raise BadRequest("agents must be one JSON object of agent definitions")


def _plugin(state: Path) -> list[str]:
    """The skills, from the folder the warden renders: never one a request names, as plugin hooks run unsandboxed."""
    return ["--plugin-dir", str(Places.for_runner(_INSTALL, state).rendered_plugin)]


def session_argv(raw: dict[str, Any], state: Path) -> list[str]:
    """A session's `claude --bg` command, built by the warden's own function, with the installed claude."""
    _value(raw, "worktree", empty=True)
    if not isinstance(raw.get("prompt"), str):
        raise BadRequest("prompt must be text")
    _carried_ok(raw, state)
    argv = launch.session_argv(raw)
    return [PROGRAMS["claude"], *_plugin(state), *argv[1:]]


def _claude_ok(args: list[str]) -> None:
    """`claude stop`, `rm` or `respawn` of one session, by its id: a respawn runs under the options it started with."""
    if len(args) != 2 or args[0] not in ("stop", "rm", "respawn") or not _SESSION_REF.match(args[1]):
        raise BadRequest("claude runs only as stop, rm or respawn of one session")


def command_argv(raw: dict[str, Any], home: Path, state: Path) -> tuple[list[str], Path]:
    """One of the few commands the warden runs as the agent, with the installed programs."""
    argv, cwd = raw.get("argv"), raw.get("cwd")
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and "\0" not in a for a in argv):
        raise BadRequest("argv must be a list of words")
    if not isinstance(cwd, str) or not os.path.isabs(cwd) or not _inside(Path(os.path.normpath(cwd)), home):
        raise BadRequest(f"cwd must be a folder in {home}")
    program, args = argv[0], argv[1:]
    if program == "claude":
        _claude_ok(args)
    elif program == "rm":
        target = Path(os.path.normpath(args[-1])) if len(args) == 3 and os.path.isabs(args[-1]) else None
        if args[:2] != ["-rf", "--"] or target is None or target == home or not _inside(target, home):
            raise BadRequest(f"rm removes only one folder below {home}")
    elif program == "kill":
        if len(args) != 2 or args[0] != "-TERM" or not args[1].isdigit():
            raise BadRequest("kill only sends TERM to one process id")
    elif program != "git":
        raise BadRequest(f"the warden never runs {program!r} as the agent")
    return [PROGRAMS[program], *args], Path(os.path.normpath(cwd))


def stack_parts(raw: dict[str, Any], settings: Settings) -> tuple[int, list[str], str]:
    slot, verb, session = raw.get("slot"), raw.get("verb"), raw.get("session")
    # A slot past a lowered slots.count still holds its lease until it is freed, so the bound is the most slots
    # the setting may ever name; the warden names only slots it leased.
    top = int(SCHEMA["slots.count"].high or 0)
    if isinstance(slot, bool) or not isinstance(slot, int) or not 0 <= slot <= top:
        raise BadRequest(f"slot must be from 0 to {top}")
    if not isinstance(verb, list) or not verb or not all(isinstance(w, str) and "\0" not in w for w in verb):
        raise BadRequest("verb must be a list of words")
    if verb[0] not in DEV_VERBS | OWN_VERBS:
        raise BadRequest(f"{verb[0]!r} is not a scripts/dev verb")
    if not isinstance(session, str):
        raise BadRequest("session must be text")
    return slot, verb, session


def check_parts(raw: dict[str, Any], home: Path) -> tuple[Path, list[str]]:
    """The checkout a check reads, a folder in the agent's home, and the check runner's arguments: its flags, each
    followed by check ids or a commit where it takes one."""
    checkout, args = raw.get("checkout"), raw.get("args")
    if not isinstance(checkout, str) or not os.path.isabs(checkout) or "\0" in checkout:
        raise BadRequest("checkout must be an absolute path")
    path = Path(os.path.normpath(checkout))
    if path == home or not _inside(path, home):
        raise BadRequest(f"checkout must be a folder in {home}")
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise BadRequest("args must be a list of words")
    words = iter(args)
    for word in words:
        if word not in CHECK_FLAGS:
            raise BadRequest(f"{word!r} is not a check flag: {', '.join(sorted(CHECK_FLAGS))}")
        if word != "--all" and not _CHECK_WORD.match(next(words, "")):
            raise BadRequest(f"{word} takes check ids or a commit")
    return path, list(args)


def checkout_for(session: str, settings: Settings, sessions: dict[str, Any]) -> Path:
    """The checkout a verb builds: the session's own worktree, or the main checkout for slot 0 and the warden."""
    repo = Path(settings.get_str("repo.path"))
    row = sessions.get(session)
    worktree = row.get("worktree") if isinstance(row, dict) else ""
    path = Path(os.path.normpath(worktree)) if isinstance(worktree, str) and worktree else repo
    if path != repo and not _inside(path, session_settings.worktrees(repo)):
        raise BadRequest(f"{path} is neither the product checkout nor one of its worktrees")
    return path


def unit_token(credentials: str | None) -> str:
    """The sign-in token systemd loaded for the unit into the folder ``credentials``."""
    if not credentials:
        raise BadRequest("the unit loaded no sign-in token")
    try:
        return (Path(credentials) / layout.CLAUDE_TOKEN).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise BadRequest(f"the sign-in token cannot be read ({type(exc).__name__})") from exc


def claude_env(inherited: dict[str, str], credentials: str | None) -> tuple[dict[str, str], tuple[int, ...]]:
    """The unit's environment for claude, and the descriptors to pass it: a pipe holding the sign-in token systemd
    loaded for the unit. Claude Code reads the pipe to its end, so the token is in no environment and no descriptor
    a shell it starts could read. Never the token in the environment with CLAUDE_CODE_SUBPROCESS_ENV_SCRUB: that also
    runs every command sandboxed, the excluded `factory` too, which then cannot reach the warden. ``inherited`` is the
    unit's own environment, handed on; ``credentials`` the folder systemd loaded the token into."""
    token = unit_token(credentials)
    read, write = os.pipe()
    with os.fdopen(write, "w", encoding="utf-8") as pipe:
        pipe.write(token + "\n")  # far below a pipe's buffer, so the write never waits for a reader
    os.set_inheritable(read, True)
    return {**inherited, TOKEN_FD: str(read)}, (read,)


def message_argv(raw: dict[str, Any]) -> list[str]:
    """`claude --resume <session> <text>`: the owner's message as the running session's next user turn."""
    session, text = raw.get("session"), raw.get("text")
    if not isinstance(session, str) or not _UUID.match(session):
        raise BadRequest("session must be a session uuid")
    # Read as a flag, a text starting with a dash would change what claude does instead of being sent.
    if not isinstance(text, str) or not text.strip() or text.startswith("-") or len(text) > 65536:
        raise BadRequest("text must be a message that does not start with a dash")
    return [PROGRAMS["claude"], "--resume", session, text]


def run_in_terminal(argv: list[str], cwd: Path, env: dict[str, str], hold: int, fds: tuple[int, ...] = ()) -> int:
    """Run ``argv`` in a pseudo-terminal for ``hold`` seconds, then let go of it.

    `claude --resume <id> <text>` on a running background session sends the text as its next turn, then attaches,
    and with no terminal it sends nothing. Ctrl+Z leaves the attached view and the session keeps running. An exit of
    its own before ``hold`` is a failure to deliver, reported by its status.
    """
    leader, follower = pty.openpty()
    try:
        proc = subprocess.Popen(argv, cwd=cwd, env={**env, "TERM": "xterm-256color"}, stdin=follower,
                                stdout=follower, stderr=follower, start_new_session=True, pass_fds=fds)  # fmt: skip
    # telemetry: surfaced — the failed start is `print`ed to the unit's log, and the exit says so
    except OSError as exc:
        os.close(leader)
        os.close(follower)
        print(f"runner: {Path(argv[0]).name} could not start ({type(exc).__name__})", flush=True)
        return REFUSED
    os.close(follower)
    seen = b""
    deadline = time.monotonic() + hold
    while time.monotonic() < deadline and proc.poll() is None:
        ready, _, _ = select.select([leader], [], [], 0.5)
        if ready:
            try:
                seen = (seen + os.read(leader, 65536))[-4096:]
            # telemetry: expected — the terminal closed: the session's output has ended
            except OSError:
                break
    if proc.poll() is None:
        os.write(leader, b"\x1a")
        try:
            proc.wait(5)
        # telemetry: expected — a process that does not stop within its five seconds is killed
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(5)
        os.close(leader)
        return 0
    os.close(leader)
    if proc.returncode != 0:
        print(f"runner: claude exited {proc.returncode}: {_plain(seen)[-300:]}", flush=True)
    return proc.returncode


def _plain(raw: bytes) -> str:
    return re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", raw.decode(errors="replace")).strip()


def run(argv: list[str], cwd: Path, env: dict[str, str], timeout: int, fds: tuple[int, ...] = ()) -> int:
    """Run with the unit's own output (the warden's runs/<id>.log); the exit status is the unit's."""
    try:
        return subprocess.run(argv, cwd=cwd, env=env, timeout=timeout, pass_fds=fds, check=False).returncode
    # telemetry: surfaced — the timeout is `print`ed to the unit's log, and the exit says so
    except subprocess.TimeoutExpired:
        print(f"runner: {Path(argv[0]).name} ran past {timeout} s and was stopped", flush=True)
        return TIMED_OUT
    # telemetry: surfaced — the failed start is `print`ed to the unit's log, and the exit says so
    except OSError as exc:
        print(f"runner: {Path(argv[0]).name} could not start ({type(exc).__name__})", flush=True)
        return REFUSED
