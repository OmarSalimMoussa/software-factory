"""The agents' Claude accounts, for the owner: list them, add or remove one, and switch the agents to another.

The active account is the one whose token the agents' Claude holds now. A switch is an operation: the account's token
is copied to the file the agent units load; the agents' Claude daemon restarts, which loads it and ends every session
the daemon hosts; the first session that was working comes back, and the switch waits for its first answer. Only an
answer proves the token signs in: when none comes, or the account is at its plan limit, the old token goes back the
same way and the step says why. Then each other session comes back through the lifecycle's own resume, told why. The
restart is the factory's own act, so while the switch runs the crash watch leaves those sessions alone, and no session
starts until the daemon is back (permits.check_daemon).

A token arrives only as a request's bytes. It is never answered, logged or put in an error: only its last characters
are shown, to tell accounts apart.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runtime.shared import claude_accounts, operations, store
from runtime.shared.claude_accounts import SHOWN, UNIT_TOKEN, AccountError
from runtime.shared.models import PENDING, REVIVING, WAITING, WORKING
from runtime.shared.protocol import Refused, ok
from runtime.system import layout
from runtime.warden.context import Context
from runtime.warden.owner import ops
from runtime.warden.sessions import backstop, lifecycle, permits, registry, respawn
from runtime.warden.sessions.launch import LaunchError
from runtime.warden.sessions.lifecycle import WARDEN
from runtime.warden.sessions.permits import BACK, ORCH, RESTARTING

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


def _active(ctx: Context, secrets: Path) -> str | None:
    """The account whose token the agents' Claude holds now; None before it first signed in, or with one not kept."""
    held = ctx.launcher.signed_in()
    for path in claude_accounts.token_files(secrets):
        name = claude_accounts.name_of(path)
        if name is not None and held is not None and _token(path) == held:
            return name
    return None


def _load(secrets: Path, token: str) -> None:
    """The token the agent units load the next time they start."""
    store.write_text(secrets / UNIT_TOKEN, token + "\n", mode=0o600)


def listing(ctx: Context) -> list[dict[str, Any]]:
    """Each account: its name, whether the agents use it, when it was added and its token's last characters. A file
    that cannot be an account's is a row that says so."""
    secrets = _secrets(ctx)
    active = _active(ctx, secrets)
    rows: list[dict[str, Any]] = []
    for path in claude_accounts.token_files(secrets):
        name = claude_accounts.name_of(path)
        if name is None:
            rows.append({"broken": True, "id": path.name, "error": "Its file name is not an account's name."})
            continue
        try:
            token, added = _token(path), path.stat().st_mtime
        # telemetry: surfaced — the account is one of the `rows`, drawn as one that cannot be read; the rest still draw
        except (OSError, ValueError) as exc:
            rows.append({"broken": True, "id": name, "error": f"Its token cannot be read ({type(exc).__name__})."})
            continue
        stamp = datetime.fromtimestamp(added, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows.append({"name": name, "active": name == active, "added": stamp, "last4": token[-SHOWN:]})
    return rows


def list_accounts(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    return ok(accounts=listing(ctx))


def add(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """{name, switch} with the token as the request's bytes. The first account is the one the agents load; another
    is switched to at once when ``switch`` says so."""
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
        first = not (secrets / UNIT_TOKEN).exists()
        if first:
            _load(secrets, token)
    if first:
        return ok(name=name, active=True, message=f"Added {name}; the agents sign in with it when they next start.")
    if args.get("switch") is True:
        try:
            started = use(ctx, {"name": name}, None)
        # telemetry: surfaced — the account is added, and the `message` says why the switch waits
        except Refused as exc:
            return ok(name=name, active=False, message=f"Added {name}, but the switch waits: {exc.reason}.")
        return {**started, "message": f"Added {name}; switching the agents to it."}
    return ok(name=name, active=False, message=f"Added {name}. Switch to it when the agents should use it.")


def remove(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    name = _name(args)
    secrets = _secrets(ctx)
    with ctx.lock:
        path = claude_accounts.token_file(secrets, name)
        if not path.exists():
            raise Refused("account", f"there is no account {name}; factory account list shows them")
        if permits.switching(ctx) is not None:
            raise Refused("switching", "a switch of accounts is running: remove it once that ends")
        if _active(ctx, secrets) == name:
            raise Refused("active", f"the agents use {name}: switch to another account first")
        path.unlink()
    return ok(name=name, message=f"Removed {name}.")


def use(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """Switch the agents to the account {name}: an operation, unless the agents use it already."""
    name = _name(args)
    secrets = _secrets(ctx)
    with ctx.lock:
        token, old = _account_token(secrets, name), _active(ctx, secrets)
        if old == name:
            return ok(name=name, message=f"The agents already use {name}; nothing changed.")
        if permits.switching(ctx) is not None:
            raise Refused("switching", "a switch of accounts is running: wait for it to finish")
        sessions = lifecycle.read(ctx)
        merging = [s.name for s in sessions.values() if s.merge_turn]
        if merging:
            raise Refused("merging", f"{', '.join(merging)} is in its merge's last turn; switch in a few minutes")
        moving = [s.name for s in sessions.values()
                  if s.state == REVIVING or (s.state == WORKING and s.id.startswith(PENDING))]  # fmt: skip
        if moving:
            raise Refused("settling", f"{', '.join(moving)} is starting or coming back now; switch again in a minute")
        live = [s for s in sessions.values() if s.state in (WORKING, WAITING) and not s.parent]
        back = [s.id for s in sorted(live, key=lambda s: (s.role != ORCH, s.started))]
        undo = _token(secrets / UNIT_TOKEN) if (secrets / UNIT_TOKEN).exists() else ""
        steps = [
            (f"Signing the agents in as {name}", lambda: _sign_in(ctx, token)),
            ("Restarting the agents' Claude daemon", lambda: _restart(ctx)),
            (f"Checking that {name} answers", lambda: _check(ctx, name, old or "the old account", undo, back)),
            ("Bringing back the sessions", lambda: _bring_back(ctx, back[1:])),
        ]
        op_id = ops.launch(ctx, operations.ACCOUNT, steps, {"name": name})
        # Written before the steps run: the first step waits for this lock.
        record = {"op": op_id, "name": name, "sessions": back, "daemon": RESTARTING}
        store.write_json(permits.switch_path(ctx), record, mode=0o600)
    return ok(name=name, op=op_id, message=f"Switching the agents to {name}.")


def _sign_in(ctx: Context, token: str) -> str:
    with ctx.lock:
        _load(_secrets(ctx), token)
    return f"its token ends in {token[-SHOWN:]}"


def _daemon(ctx: Context, state: str) -> None:
    with ctx.lock:
        record = store.read_json(permits.switch_path(ctx))
        store.write_json(permits.switch_path(ctx), {**record, "daemon": state}, mode=0o600)


def _restart(ctx: Context) -> str:
    _daemon(ctx, RESTARTING)
    try:
        ctx.launcher.restart_daemon()
    except LaunchError as exc:
        raise ops.StepError(str(exc)) from exc
    _daemon(ctx, BACK)
    return ""


def _check(ctx: Context, name: str, old: str, undo: str, back: list[str]) -> str:
    """The first session comes back and answers on the new account, or the old token goes back with every session."""
    if not back:
        return "no agent was working to check it with: the next one to start shows whether it signs in"
    settings, since = ctx.settings(), ctx.clock.now()
    _bring_back(ctx, back[:1])
    deadline = since + settings.get_int("watch.account_check_seconds")
    while (said := ctx.launcher.answered(back[0], since)) is None and ctx.clock.now() < deadline:
        ctx.clock.sleep(settings.get_int("watch.respawn_poll_seconds"))
    if said == "ok":
        return "the first agent back answered"
    why = {None: "no answer came", "limit": "it is at its plan limit"}.get(said, f"it did not sign in: {said}")
    _sign_in(ctx, undo)
    _restart(ctx)
    _bring_back(ctx, back)
    raise ops.StepError(f"{name}: {why}, so the agents are back on {old}")


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
                respawn.resume(ctx, s, NOTE)
            # telemetry: surfaced — a session that did not come back is one of the `failed` the owner reads
            except (LaunchError, Refused) as exc:
                failed.append(f"{s.name} did not come back ({exc})")
            else:
                lifecycle.move(ctx, s, s.state, WARDEN, "resumed after the factory switched Claude accounts")
                back.append(s.name)
            registry.write(ctx.places, sessions)
    if failed:
        raise ops.StepError("; ".join(failed) + "; the crash watch resumes them as it would after a crash")
    return f"back: {', '.join(back)}" if back else "no agent was working"
