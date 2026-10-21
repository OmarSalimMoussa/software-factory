"""`factory qa-browser <role>`: a tester's own browser, which Claude Code starts from the tester's agent definition
when the tester starts, and stops when it finishes.

It makes the run's folder, writes Playwright's settings into it, then runs Playwright's MCP server behind the
wrapper (wrapper.py), which talks to Claude Code over standard input and output, so nothing here prints to standard
output; when the server stops, the wrapper stops too, so the session sees its browser gone. The wrapper traces the
whole run: every frame the screen showed and every action, on the browser's own clock, from which the warden builds
the run's replay. A page recorder (recorder.js) runs in every page and notes where each click landed and what each
read touched. The tester's second browser (second.py) writes into the run's ``second/``. When the tester reports its
result, or the orchestrator saves the check a regression tester walked for, the warden copies the folder into its
evidence, where only the owner looks; its clean-up removes a run nothing took.
"""

from __future__ import annotations

import json
import os
import pwd
import re
import secrets
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import IO, Any

from runtime.qa import second as second_module
from runtime.qa.wrapper import CLOSE, FIRST, SECOND, Proxy
from runtime.qa.wrapper_tools import NOTES, TOOLS, ToolError, Tools, first_link
from runtime.shared import environment, lanes, store
from runtime.shared import settings as settings_module
from runtime.shared.paths import AGENT_USER, Places
from runtime.shared.settings import Settings

# A pinned release: a new one is a factory change, tested like any other.
PLAYWRIGHT_MCP = "@playwright/mcp@0.0.77"
RUNS = ".factory-qa"
# A run's folder: when it started, then a few random letters so two testers started in one second never share one.
RUN = re.compile(r"^(\d{8}T\d{6}Z)-[0-9a-f]{6}$")
SESSION = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
STAMP = "%Y%m%dT%H%M%SZ"
RECORDER = Path(__file__).resolve().with_name("recorder.js")
# The one-time links the wrapper signed the browser in with, kept beside the run for the replay to scrub, then gone.
SECRETS = "secrets.json"


class BrowserError(Exception):
    """The tester's browser cannot start; the message says why."""


def runs_root() -> Path:
    """Where the agent user's testers keep their runs until the warden takes them."""
    return Path(pwd.getpwnam(AGENT_USER).pw_dir) / RUNS


def run_folder(root: Path, session_id: str, now: datetime) -> Path:
    if not SESSION.match(session_id):
        raise BrowserError(f"the session id {session_id!r} is not one Claude Code gives")
    folder = root / session_id / f"{now.strftime(STAMP)}-{secrets.token_hex(3)}"
    folder.mkdir(parents=True, mode=0o700)
    return folder


def blocked(settings: Settings) -> list[str]:
    """The pages no tester's browser may open: the owner's dashboard, by every name it answers to."""
    port = settings.get_int("dashboard.port")
    return [f"http://{host}:{port}" for host in settings.get_list("dashboard.hosts")] + [
        f"https://{host}" for host in settings.get_list("dashboard.hosts")
    ]


def playwright_settings(folder: Path, role: str, settings: Settings, endpoint: str | None = None) -> dict[str, Any]:
    """Chrome headless with a private profile, every file in ``folder``, and no screenshot shown to a tester whose
    screenshots are the owner's only. The trace's frames are the run's recording, so no video is made. With
    ``endpoint``, a context of its own in the browser whose process answers there, as the second browser is."""
    unseen = role in settings.get_list("qa.screenshots_for_owner_only")
    viewport = {"width": int(settings.get_list("qa.widths")[0]), "height": settings.get_int("qa.viewport_height")}
    return {
        "browser": {
            "browserName": "chromium",
            "isolated": True,
            "launchOptions": {"channel": "chrome", "headless": True},
            "contextOptions": {"viewport": viewport, "permissions": settings.get_list("qa.browser_permissions")},
            **({"remoteEndpoint": endpoint} if endpoint else {}),
        },
        # Vision clicks at a point, which a remote desktop needs: it is a video of a screen. Devtools holds the
        # trace's tools, which only the wrapper sees. Network lets a step answer a request with the status it asks
        # for, such as a 502; the guard keeps each route answering what it catches, never sending it on.
        "capabilities": ["core", "vision", "devtools", "network"],
        "outputDir": str(folder),
        "imageResponses": "omit" if unseen else "allow",
        "network": {"blockedOrigins": blocked(settings)},
    }


def prepare(role: str, session_id: str, root: Path, settings: Settings, now: datetime) -> Path:
    """The run's folder, with who ran it and Playwright's settings written in it; returns the settings file."""
    if lanes.kind_of(role) not in settings.get_list("subagents.browser_personas"):
        raise BrowserError(f"{role!r} is not a tester with a browser")
    folder = run_folder(root, session_id, now)
    meta = {"role": role, "session_id": session_id, "started": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
    (folder / "run.json").write_text(json.dumps(meta), encoding="utf-8")
    config = folder / "playwright.json"
    config.write_text(json.dumps(playwright_settings(folder, lanes.kind_of(role), settings)), encoding="utf-8")
    return config


def published(places: Places) -> Settings:
    return settings_module.build(store.read_json(places.state / "public" / "settings.json"), {})


def server_command(config: Path) -> list[str]:
    return ["npx", "-y", PLAYWRIGHT_MCP, "--config", str(config), "--init-script", str(RECORDER)]


def second_command(folder: Path, role: str, settings: Settings) -> Callable[[str], tuple[list[str], Path]]:
    """How the second browser's server starts, given where the first browser's process answers: in a folder of its
    own in the run's, which holds its settings and every file it writes."""

    def command(endpoint: str) -> tuple[list[str], Path]:
        where = folder / SECOND
        where.mkdir(mode=0o700, exist_ok=True)
        config = where / "playwright.json"
        config.write_text(json.dumps(playwright_settings(where, role, settings, endpoint)), encoding="utf-8")
        return server_command(config), where

    return command


def keeper(folder: Path) -> Callable[[str], None]:
    """Notes a secret in the run's folder, readable by its owner only."""

    def keep(secret: str) -> None:
        path = folder / SECRETS
        kept = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        # Made readable by its owner only from the start: the link signs anyone in until it is used.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps([*kept, secret]))

    return keep


def link_asker(sign_in: Callable[[str], dict[str, Any]]) -> Callable[[str], str]:
    """A fresh one-time link for a test address, from the slot's own sign-in, which the warden runs."""

    def link_for(address: str) -> str:
        answer = sign_in(address)
        if not answer.get("ok"):
            raise ToolError(f"the warden refused the sign-in: {answer.get('reason', 'no reason given')}")
        if answer.get("exit") != 0:
            raise ToolError(f"your slot's sign-in exited {answer.get('exit')}; report the step blocked with that")
        return first_link(str(answer.get("tail", "")))

    return link_for


def main(argv: list[str], places: Places, sign_in: Callable[[str], dict[str, Any]]) -> int:
    """``sign_in`` asks the warden to run the slot's sign-in for a test address, as the session's own call."""
    if len(argv) != 1:
        print("factory qa-browser <role>", file=sys.stderr)
        return 2
    try:
        session_id = environment.claude_session_id()  # Claude Code names the session it starts this server for
        if not session_id:
            raise BrowserError("Claude Code gave no session id (CLAUDE_CODE_SESSION_ID)")
        settings = published(places)
        config = prepare(argv[0], session_id, Path.home() / RUNS, settings, datetime.now(UTC))
    except (BrowserError, settings_module.SettingsError, store.StoreError, OSError) as exc:
        print(f"factory qa-browser: no browser: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    os.chdir(config.parent)
    second = second_command(config.parent, lanes.kind_of(argv[0]), settings)
    equip = equipper(link_asker(sign_in), keeper(config.parent), settings, second)
    return serve(
        server_command(config), equip, settings.get_int("qa.browser_answer_seconds"),
        settings.get_int("qa.browser_answer_chars"),
    )  # fmt: skip


def equipper(
    link_for: Callable[[str], str],
    keep: Callable[[str], None],
    settings: Settings,
    second: Callable[[str], tuple[list[str], Path]],
) -> Callable[[Proxy], None]:
    """What the wrapper adds to the browser: the factory's tools in either browser, the second browser ``second``
    starts, and every link they sign in with kept beside the run for the replay and hidden from the session."""
    width, height = (int(n) for n in settings.get_list("qa.desktop_viewport"))

    def equip(proxy: Proxy) -> None:
        def kept(secret: str) -> None:
            keep(secret)
            proxy.hide(secret)

        opened = second_module.Second(proxy, second, settings.get_int("qa.browser_answer_seconds"))
        proxy.add_second(opened)
        tools = {
            which: Tools(
                partial(proxy.call_tool, which=which),
                link_for,
                kept,
                partial(proxy.answers.signed_in, which),
                settings.get_int("qa.wait_reload_seconds"),
                settings.get_int("qa.wait_max_minutes"),
                (width, height),
                settings.get_int("qa.key_delay_ms"),
                settings.get_int("qa.hold_max_seconds"),
                settings.get_int("qa.click_hold_ms"),
                settings.get_int("qa.press_settle_ms"),
                time.monotonic,
            )
            for which in (FIRST, SECOND)
        }

        def answer(name: str, arguments: dict[str, Any], cancelled: threading.Event, which: str) -> tuple[str, bool]:
            return opened.close() if name == CLOSE else tools[which].answer(name, arguments, cancelled)

        proxy.add_tools([*TOOLS, second_module.TOOL], answer, NOTES)

    return equip


def serve(
    argv: list[str],
    equip: Callable[[Proxy], None],
    wait_seconds: float,
    answer_chars: int,
    client_in: IO[str] = sys.stdin,
    client_out: IO[str] = sys.stdout,
) -> int:
    """Run the server as a child, let ``equip`` add the factory's tools, and carry the session's traffic until the
    session closes standard input or the server stops; the server's exit code, or 1 when the trace did not start."""
    server = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
    assert server.stdin is not None and server.stdout is not None  # both were asked for as pipes
    proxy = Proxy(server.stdin, client_out, wait_seconds, answer_chars)
    equip(proxy)
    ended, why = threading.Event(), []

    def untraced(reason: str) -> None:
        why.append(f"the trace did not start, so this browser stops: {reason}")
        ended.set()

    proxy.untraced = untraced

    def pump() -> None:
        assert server.stdout is not None  # asked for as a pipe
        for line in server.stdout:
            if line.strip():
                proxy.from_server(line)
        why.append("server")
        ended.set()

    def feed() -> None:
        try:
            for line in client_in:
                if line.strip():
                    proxy.from_client(line)
            proxy.traced()
            why.append("session")
        # telemetry: surfaced — the server is gone; `why` says so, and `serve` reports it on stderr
        except OSError:
            why.append("server")
        ended.set()

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    threading.Thread(target=feed, daemon=True).start()
    ended.wait()
    proxy.close()
    if why[0] == "session":
        server.stdin.close()
        reader.join()  # the server's last answers still reach the session
        return server.wait()
    server.kill()
    said = why[0] if why[0] != "server" else f"the browser stopped (exit {server.wait()})"
    print(f"factory qa-browser: {said}", file=sys.stderr)
    return 1
