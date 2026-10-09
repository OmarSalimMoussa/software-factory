"""What a template unit's runner reads: one request the warden wrote to public/launch/<id>.json, checked before
anything runs, and the effective settings and sessions the warden published beside it.

The warden is the only writer of public/, so these checks are a second wall: a request that names a program other
than claude or git, a path outside the agent's home or the product checkout, a flag where a value belongs, or a slot
that does not exist is refused, and the runner exits without running anything.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from runtime.shared.settings import Settings
from runtime.system import layout
from runtime.warden import launch, session_settings

SESSION, COMMAND, STACK = "session", "command", "stack"
REFUSED = 2
TIMED_OUT = 124
_ID = re.compile(r"^[A-Za-z0-9_-]{1,96}$")
PROGRAMS = {"claude": str(layout.CLAUDE_BIN), "git": "/usr/bin/git"}
# The verbs of the product's scripts/dev (factory spec, "scripts/dev"; plan R24 for reports).
DEV_VERBS = frozenset({"up", "down", "empty", "health", "sign-in", "seed", "desktop", "test", "reports", "verbs"})
SWEEP, PRUNE, DISK = "factory-sweep-slot", "factory-prune", "factory-disk"
OWN_VERBS = frozenset({SWEEP, PRUNE, DISK})
_SESSION_FIELDS = ("session_id", "name", "model", "effort", "agent", "settings", "agents", "worktree")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


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


def session_argv(raw: dict[str, Any], state: Path) -> list[str]:
    """A session's `claude --bg` command, built by the warden's own function, with the installed claude."""
    for key in _SESSION_FIELDS:
        _value(raw, key, empty=key == "worktree")
    if not isinstance(raw.get("prompt"), str):
        raise BadRequest("prompt must be text")
    if not _UUID.match(raw["session_id"]):
        raise BadRequest("session_id must be a session uuid")
    public = layout.public(state)
    for key, folder in (("settings", public / "session-settings"), ("agents", public / "plugin")):
        if not _inside(Path(os.path.normpath(raw[key])), folder) or not raw[key].endswith(".json"):
            raise BadRequest(f"{key} must be a file the warden wrote in {folder}")
    argv = launch.session_argv(raw)
    return [PROGRAMS["claude"], *argv[1:]]


def command_argv(raw: dict[str, Any], home: Path) -> tuple[list[str], Path]:
    argv, cwd = raw.get("argv"), raw.get("cwd")
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and "\0" not in a for a in argv):
        raise BadRequest("argv must be a list of words")
    if argv[0] not in PROGRAMS:
        raise BadRequest(f"a command runs {' or '.join(PROGRAMS)}, never {argv[0]!r}")
    if not isinstance(cwd, str) or not os.path.isabs(cwd) or not _inside(Path(os.path.normpath(cwd)), home):
        raise BadRequest(f"cwd must be a folder in {home}")
    return [PROGRAMS[argv[0]], *argv[1:]], Path(os.path.normpath(cwd))


def stack_parts(raw: dict[str, Any], settings: Settings) -> tuple[int, list[str], str]:
    slot, verb, session = raw.get("slot"), raw.get("verb"), raw.get("session")
    if isinstance(slot, bool) or not isinstance(slot, int) or not 0 <= slot <= settings.get_int("slots.count"):
        raise BadRequest(f"slot must be from 0 to {settings.get_int('slots.count')}")
    if not isinstance(verb, list) or not verb or not all(isinstance(w, str) and "\0" not in w for w in verb):
        raise BadRequest("verb must be a list of words")
    if verb[0] not in DEV_VERBS | OWN_VERBS:
        raise BadRequest(f"{verb[0]!r} is not a scripts/dev verb")
    if not isinstance(session, str):
        raise BadRequest("session must be text")
    return slot, verb, session


def checkout_for(session: str, settings: Settings, sessions: dict[str, Any]) -> Path:
    """The checkout a verb builds: the session's own worktree, or the main checkout for slot 0 and the warden."""
    repo = Path(settings.get_str("repo.path"))
    row = sessions.get(session)
    worktree = row.get("worktree") if isinstance(row, dict) else ""
    path = Path(os.path.normpath(worktree)) if isinstance(worktree, str) and worktree else repo
    if path != repo and not _inside(path, session_settings.worktrees(repo)):
        raise BadRequest(f"{path} is neither the product checkout nor one of its worktrees")
    return path


def claude_env(environ: dict[str, str]) -> dict[str, str]:
    """The unit's environment with the sign-in token systemd loaded for it; Claude Code scrubs it from shells."""
    folder = environ.get("CREDENTIALS_DIRECTORY")
    if not folder:
        raise BadRequest("the unit loaded no sign-in token")
    try:
        token = (Path(folder) / layout.CLAUDE_TOKEN).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise BadRequest(f"the sign-in token cannot be read ({type(exc).__name__})") from exc
    return {**environ, "CLAUDE_CODE_OAUTH_TOKEN": token, "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1"}


def run(argv: list[str], cwd: Path, env: dict[str, str], timeout: int) -> int:
    """Run with the unit's own output (the warden's runs/<id>.log); the exit status is the unit's."""
    try:
        return subprocess.run(argv, cwd=cwd, env=env, timeout=timeout, check=False).returncode
    except subprocess.TimeoutExpired:
        print(f"runner: {Path(argv[0]).name} ran past {timeout} s and was stopped", flush=True)
        return TIMED_OUT
    except OSError as exc:
        print(f"runner: {Path(argv[0]).name} could not start ({type(exc).__name__})", flush=True)
        return REFUSED
