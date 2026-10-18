"""`factory qa-browser <role>`: a tester's own browser, which Claude Code starts from the tester's agent definition
when the tester starts, and stops when it finishes.

It makes the run's folder, writes Playwright's settings into it, then runs Playwright's MCP server behind the
wrapper (wrapper.py), which talks to Claude Code over standard input and output, so nothing here prints to standard
output. The wrapper traces the whole run: every frame the screen showed and every action, on the browser's own clock,
from which the warden builds the run's replay. A page recorder (recorder.js) runs in every page and notes where each
click landed and what each read touched. When the tester reports its result, or the orchestrator saves the check a
regression tester walked for, the warden copies the folder into its evidence, where only the owner looks; its
clean-up removes a run nothing took.
"""

from __future__ import annotations

import json
import os
import pwd
import re
import secrets
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runtime.qa import wrapper
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


def playwright_settings(folder: Path, role: str, settings: Settings) -> dict[str, Any]:
    """Chrome headless with a private profile, every file in the run's folder, and no screenshot shown to a tester
    whose screenshots are the owner's only. The trace's frames are the run's recording, so no video is made."""
    unseen = role in settings.get_list("qa.screenshots_for_owner_only")
    viewport = {"width": int(settings.get_list("qa.widths")[0]), "height": settings.get_int("qa.viewport_height")}
    return {
        "browser": {
            "browserName": "chromium",
            "isolated": True,
            "launchOptions": {"channel": "chrome", "headless": True},
            "contextOptions": {"viewport": viewport},
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
    equip = equipper(link_asker(sign_in), keeper(config.parent), settings)
    return wrapper.serve(server_command(config), equip, settings.get_int("qa.browser_answer_seconds"))


def equipper(
    link_for: Callable[[str], str], keep: Callable[[str], None], settings: Settings
) -> Callable[[wrapper.Proxy], None]:
    """What the wrapper adds to the browser: the factory's tools, and every link they sign in with kept beside the
    run for the replay and hidden from the session from then on."""
    width, height = (int(n) for n in settings.get_list("qa.desktop_viewport"))

    def equip(proxy: wrapper.Proxy) -> None:
        def kept(secret: str) -> None:
            keep(secret)
            proxy.hide(secret)

        tools = Tools(
            proxy.call_tool,
            link_for,
            kept,
            settings.get_int("qa.wait_reload_seconds"),
            settings.get_int("qa.wait_max_minutes"),
            (width, height),
            settings.get_int("qa.key_delay_ms"),
            settings.get_int("qa.hold_max_seconds"),
            settings.get_int("qa.click_hold_ms"),
            settings.get_int("qa.press_settle_ms"),
            time.monotonic,
        )
        proxy.add_tools(TOOLS, tools.answer, NOTES)

    return equip
