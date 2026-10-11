"""The backstop: no Claude session of the agent user runs without the warden's say-so, for more than one tick.

Claude Code writes one record per session process under the agent user's ``~/.claude/sessions``, naming the
session's id, its name and its chat link; that is how the warden sees every session, whatever started it. Each tick
a new session whose name a pending start is waiting for is bound to that start; any other session the registry does
not hold, or holds as retired or deprioritized, is stopped. A session being benched or revived is never touched.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from runtime.shared import store
from runtime.shared.models import BENCH_PENDING, DEPRIORITIZED, PENDING, RETIRED, REVIVING, Session
from runtime.warden.context import Context
from runtime.warden.sessions import launch, registry
from runtime.warden.sessions.launch import LaunchError

log = logging.getLogger(__name__)
CHAT = "https://claude.ai/code/"
PROC = Path("/proc")


@dataclass(frozen=True, slots=True)
class Proc:
    pid: int
    session: str
    name: str
    chat_link: str
    status: str = ""  # Claude Code's own word for the session, such as idle


def _started(proc: Path, pid: int) -> str:
    try:
        stat = (proc / str(pid) / "stat").read_text()
    # telemetry: expected — the process ended, so its record names no live session
    except OSError:
        return ""
    # Field 22 of stat is the start time; the fields after "pid (comm)" begin at field 3.
    fields = stat.rsplit(")", 1)[-1].split()
    return fields[19] if len(fields) > 19 else ""


def read_processes(folder: Path, proc: Path) -> dict[str, Proc]:
    """Each session with a running process, by session id.

    A record whose process has ended, or whose pid now belongs to another process (the kernel's start time differs
    from the recorded ``procStart``), is skipped.
    """
    found: dict[str, Proc] = {}
    for path in folder.glob("*.json"):
        try:
            record = json.loads(path.read_text())
        # telemetry: expected — a record is removed or rewritten as its process ends
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict) or not isinstance(record.get("sessionId"), str):
            continue
        pid = record.get("pid")
        if not isinstance(pid, int) or _started(proc, pid) != str(record.get("procStart")):
            continue
        bridge = str(record.get("bridgeSessionId") or "")
        link = CHAT + bridge if bridge.startswith("session_") else ""
        name, status = str(record.get("name") or ""), str(record.get("status") or "")
        found[record["sessionId"]] = Proc(pid, record["sessionId"], name, link, status)
    return found


def processes() -> dict[str, Proc]:
    return read_processes(sessions_folder(), PROC)


def daemon_started() -> float | None:
    """When the agent's Claude daemon, which hosts every background session, started, in seconds since the epoch;
    None while it is not running."""
    uid = launch.agent_uid()
    if uid is None:
        return None
    boot = next(int(line.split()[1]) for line in (PROC / "stat").read_text().splitlines() if line.startswith("btime "))
    ticks, found = os.sysconf("SC_CLK_TCK"), []
    for entry in (e for e in PROC.iterdir() if e.name.isdigit()):
        try:
            if entry.stat().st_uid != uid:
                continue
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        # telemetry: expected — the process ended while it was read
        except OSError:
            continue
        started = _started(PROC, int(entry.name))
        if argv[0].endswith(b"claude") and argv[1:3] == [b"daemon", b"run"] and started.isdigit():
            found.append(boot + int(started) / ticks)
    return min(found) if found else None


def sessions_folder() -> Path:
    return launch.agent_home() / ".claude" / "sessions"


def unseen(folder: Path, proc: Path, agent_uid: int, settled: float) -> list[int]:
    """The agent's claude processes running for at least ``settled`` seconds that no session record names: sessions
    the warden cannot see, as when a Claude Code update moved or reshaped its records. Neither its daemon nor a
    one-shot `claude -p` (the install's drills) writes a record, so neither counts. OSError or ValueError when /proc
    cannot be read."""
    named = {p.pid for p in read_processes(folder, proc).values()}
    uptime = float((proc / "uptime").read_text().split()[0])
    entries = [e for e in proc.iterdir() if e.name.isdigit()]
    ticks = os.sysconf("SC_CLK_TCK")
    found = []
    for entry in entries:
        try:
            if entry.stat().st_uid != agent_uid:
                continue
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        # telemetry: expected — the process ended while it was read
        except OSError:
            continue
        if not argv[0].endswith(b"claude") or argv[1:3] == [b"daemon", b"run"] or {b"-p", b"--print"} & set(argv):
            continue
        started = _started(proc, int(entry.name))
        if int(entry.name) not in named and started.isdigit() and uptime - int(started) / ticks >= settled:
            found.append(int(entry.name))
    return sorted(found)


def bind(sessions: dict[str, Session], procs: dict[str, Proc]) -> tuple[bool, dict[str, str]]:
    """Give each pending start the id its session got, matched by name, and keep chat links current.

    Whether anything changed, and each pending id bound, with the id it became.
    """
    changed, bound = False, {}
    for proc in procs.values():
        known = sessions.get(proc.session)
        pending = [s for s in sessions.values()
                   if s.id.startswith(PENDING) and s.name == proc.name and s.state != RETIRED]  # fmt: skip
        if known is None and len(pending) == 1:
            known = sessions.pop(pending[0].id)
            bound[known.id] = proc.session
            known.id = proc.session
            sessions[proc.session] = known
            changed = True
        if known is None:
            continue
        if proc.chat_link and known.chat_link != proc.chat_link:
            known.chat_link = proc.chat_link
            changed = True
    return changed, bound


def unpermitted(sessions: dict[str, Session], proc: Proc) -> bool:
    known = sessions.get(proc.session)
    if known is None:
        return True
    if known.state in (BENCH_PENDING, REVIVING):
        return False
    return known.state in (RETIRED, DEPRIORITIZED)


def stop(ctx: Context, proc: Proc) -> None:
    try:
        ctx.launcher.stop_session(proc.session)
        return
    except LaunchError as exc:
        log.warning("backstop: claude stop failed for session %s (%s); killing pid %d",
                    proc.session, type(exc).__name__, proc.pid)  # fmt: skip
    code, _ = ctx.launcher.agent_command(["kill", "-TERM", str(proc.pid)], ctx.settings().get_str("repo.path"))
    if code != 0:
        log.warning("backstop: session %s (pid %d) could not be stopped; tried again next tick", proc.session, proc.pid)


def sweep(ctx: Context, procs: dict[str, Proc]) -> list[Proc]:
    """Bind pending starts, then stop every session the warden did not permit; the sessions it stopped."""
    with ctx.lock:
        try:
            sessions = registry.read(ctx.places)
        except (store.StoreError, ValueError) as exc:
            # Without the registry every session looks unpermitted; stopping them all would be worse than waiting.
            log.warning("backstop: skipped this tick, the registry cannot be read (%s)", type(exc).__name__)
            return []
        changed, bound = bind(sessions, procs)
        if changed:
            registry.write(ctx.places, sessions)
        for was, sid in bound.items():
            # A start answered with its pending id; this line lets a later request still name it by that id.
            store.append_jsonl(
                ctx.places.history,
                {
                    "at": ctx.clock.iso(),
                    "session": sid,
                    "was": was,
                    "name": sessions[sid].name,
                    "by": "warden",
                    "why": "bound",
                },
            )
        strays = [p for p in procs.values() if unpermitted(sessions, p)]
        for proc in strays:
            log.warning("backstop: stopping session %s (pid %d), which runs without a permit", proc.session, proc.pid)
            stop(ctx, proc)
    return strays
