"""The agents' Claude accounts, for the owner: list them, add or remove one, and switch the agents to another.

A switch is an operation of three steps. The account's token is copied to the file the agent units load; the agents'
Claude daemon restarts, which loads it and ends every session the daemon hosts; then each session that was working or
waiting comes back through the lifecycle's own resume, told why. The restart is the factory's own act, so while the
switch runs the crash watch leaves those sessions alone (no resume counted, no incident, no `crashed` event), and no
session starts until the daemon is back (permits.check_daemon). A session that cannot come back says why in the step,
and is the crash watch's again once the switch ends.

A token arrives only as a request's bytes. It is never answered, logged or put in an error: only its last characters
are shown, to tell accounts apart.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runtime.shared import claude_accounts, store
from runtime.shared.claude_accounts import ACTIVE, SHOWN, UNIT_TOKEN, AccountError
from runtime.shared.models import REVIVING, WAITING, WORKING
from runtime.shared.protocol import Refused, ok
from runtime.system import layout
from runtime.warden import backstop, launch, lifecycle, ops, permits, registry
from runtime.warden.context import Context
from runtime.warden.launch import LaunchError
from runtime.warden.lifecycle import WARDEN
from runtime.warden.permits import BACK, ORCH, RESTARTING

KIND = "account"
NOTE = "The factory switched Claude accounts; carry on where you left off."


def _secrets(ctx: Context) -> Path:
    return layout.secrets(ctx.places.state)


def _name(args: dict[str, Any]) -> str:
    try:
        return claude_accounts.check_name(args.get("name"))
    except AccountError as exc:
        raise Refused("args", str(exc)) from exc


def _token(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def _account_token(secrets: Path, name: str) -> str:
    try:
        return _token(claude_accounts.token_file(secrets, name))
    except FileNotFoundError as exc:
        raise Refused("account", f"there is no account {name}; factory account list shows them") from exc


def _active(secrets: Path) -> str | None:
    try:
        return (claude_accounts.folder(secrets) / ACTIVE).read_text(encoding="utf-8").strip()
    # telemetry: expected — no account was made active yet
    except FileNotFoundError:
        return None


def _make_active(secrets: Path, name: str, token: str) -> None:
    # The token the units load first: cut short between the two, a switch to either account rewrites both.
    store.write_text(secrets / UNIT_TOKEN, token + "\n", mode=0o600)
    store.write_text(claude_accounts.folder(secrets) / ACTIVE, name + "\n", mode=0o600)


def listing(ctx: Context) -> list[dict[str, Any]]:
    """Each account: its name, whether the agents use it, when it was added and its token's last characters. A file
    that cannot be an account's is a row that says so."""
    secrets = _secrets(ctx)
    active = _active(secrets)
    rows: list[dict[str, Any]] = []
    for path in claude_accounts.token_files(secrets):
        name = claude_accounts.name_of(path)
        if name is None:
            rows.append({"broken": True, "id": path.name, "error": "Its file name is not an account's name."})
            continue
        try:
            token, added = _token(path), path.stat().st_mtime
        # telemetry: surfaced — the account draws as a row that cannot be read; the others still draw
        except (OSError, ValueError) as exc:
            rows.append({"broken": True, "id": name, "error": f"Its token cannot be read ({type(exc).__name__})."})
            continue
        stamp = datetime.fromtimestamp(added, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows.append({"name": name, "active": name == active, "added": stamp, "last4": token[-SHOWN:]})
    return rows


def list_accounts(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    return ok(accounts=listing(ctx))


def add(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """{name} with the token as the request's bytes; the first account added becomes the active one."""
    name = _name(args)
    if not data:
        raise Refused("token", "send the account's token: paste what `claude setup-token` printed")
    try:
        token = claude_accounts.check_token(data.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise Refused("token", "the token is not text: paste what `claude setup-token` printed") from exc
    except AccountError as exc:
        raise Refused("token", str(exc)) from exc
    secrets = _secrets(ctx)
    path = claude_accounts.token_file(secrets, name)
    with ctx.lock:
        if path.exists():
            raise Refused("name-taken", f"there is already an account {name}: pick another name, or remove it first")
        claude_accounts.folder(secrets).mkdir(mode=0o700, parents=True, exist_ok=True)
        store.write_text(path, token + "\n", mode=0o600)
        first = _active(secrets) is None
        if first:
            _make_active(secrets, name, token)
    if first:
        return ok(name=name, active=True, message=f"Added {name}; the agents use it.")
    return ok(name=name, active=False, message=f"Added {name}. Switch to it when the agents should use it.")


def remove(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    name = _name(args)
    secrets = _secrets(ctx)
    with ctx.lock:
        path = claude_accounts.token_file(secrets, name)
        if not path.exists():
            raise Refused("account", f"there is no account {name}; factory account list shows them")
        if _active(secrets) == name:
            raise Refused("active", f"the agents use {name}: switch to another account first")
        path.unlink()
    return ok(name=name, message=f"Removed {name}.")


def use(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """Switch the agents to the account {name}: an operation, unless the agents use it already."""
    name = _name(args)
    secrets = _secrets(ctx)
    with ctx.lock:
        token = _account_token(secrets, name)
        unit = secrets / UNIT_TOKEN
        if _active(secrets) == name and unit.exists() and _token(unit) == token:
            return ok(name=name, message=f"The agents already use {name}; nothing changed.")
        if permits.switching(ctx) is not None:
            raise Refused("switching", "a switch of accounts is running: wait for it to finish")
        sessions = lifecycle.read(ctx)
        moving = [s.name for s in sessions.values()
                  if s.state == REVIVING or (s.state == WORKING and s.id.startswith(launch.PENDING))]  # fmt: skip
        if moving:
            raise Refused("settling", f"{', '.join(moving)} is starting or coming back now; switch again in a minute")
        live = [s for s in sessions.values() if s.state in (WORKING, WAITING) and not s.parent]
        back = [s.id for s in sorted(live, key=lambda s: (s.role != ORCH, s.started))]
        steps = [
            (f"Making {name} the active account", lambda: _activate(ctx, name)),
            ("Restarting the agents' Claude daemon", lambda: _restart(ctx)),
            ("Bringing back the sessions", lambda: _bring_back(ctx, back)),
        ]
        op_id = ops.launch(ctx, KIND, steps, {"name": name})
        # Written before the steps run: the first step waits for this lock.
        record = {"op": op_id, "name": name, "sessions": back, "daemon": RESTARTING}
        store.write_json(permits.switch_path(ctx), record, mode=0o600)
    return ok(name=name, op=op_id, message=f"Switching the agents to {name}.")


def _activate(ctx: Context, name: str) -> str:
    secrets = _secrets(ctx)
    with ctx.lock:
        token = _account_token(secrets, name)
        _make_active(secrets, name, token)
    return f"its token ends in {token[-SHOWN:]}"


def _restart(ctx: Context) -> str:
    try:
        ctx.launcher.restart_daemon()
    except LaunchError as exc:
        raise ops.StepError(str(exc)) from exc
    with ctx.lock:
        record = store.read_json(permits.switch_path(ctx))
        store.write_json(permits.switch_path(ctx), {**record, "daemon": BACK}, mode=0o600)
    return ""


def _bring_back(ctx: Context, ids: list[str]) -> str:
    """Resume each session the restart ended, as `factory resume` would. One the owner stopped or paused meanwhile
    stays as it is; the subagents of each ended with it."""
    back: list[str] = []
    failed: list[str] = []
    for sid in ids:
        with ctx.lock:
            sessions = lifecycle.read(ctx)
            s = sessions.get(sid)
            if s is None or s.state not in (WORKING, WAITING):
                continue
            if sid in backstop.processes():
                back.append(s.name)
                continue
            lifecycle.end_children(ctx, sessions, s, "ended when the factory switched Claude accounts")
            try:
                launch.resume(ctx, s, NOTE)
            except (LaunchError, Refused) as exc:
                failed.append(f"{s.name} did not come back ({exc})")
            else:
                lifecycle.move(ctx, s, s.state, WARDEN, "resumed after the factory switched Claude accounts")
                back.append(s.name)
            registry.write(ctx.places, sessions)
    if failed:
        raise ops.StepError("; ".join(failed) + "; the crash watch resumes them as it would after a crash")
    return f"back: {', '.join(back)}" if back else "no agent was working"
