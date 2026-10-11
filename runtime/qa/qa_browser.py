"""`factory qa-browser <role>`: a tester's own browser, which Claude Code starts from the tester's agent definition
when the tester starts, and stops when it finishes.

It makes the run's folder, deletes runs older than ``qa.evidence_days``, writes Playwright's settings into the
folder, then becomes Playwright's MCP server, which talks to Claude Code over standard input and output, so nothing
here prints to standard output. The browser records a video of every page and saves every file into the folder;
when the tester reports its result, the warden copies the folder into its evidence, where only the owner looks.
"""

from __future__ import annotations

import json
import os
import pwd
import re
import secrets
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from runtime.shared import environment, store
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
    """Chrome headless with a private profile, a video of every page, every file in the run's folder, and no
    screenshot shown to a tester whose screenshots are the owner's only."""
    unseen = role in settings.get_list("qa.screenshots_for_owner_only")
    viewport = {"width": int(settings.get_list("qa.widths")[0]), "height": settings.get_int("qa.viewport_height")}
    return {
        "browser": {
            "browserName": "chromium",
            "isolated": True,
            "launchOptions": {"channel": "chrome", "headless": True},
            "contextOptions": {"viewport": viewport, "recordVideo": {"dir": str(folder / "video"), "size": viewport}},
        },
        # Vision clicks at a point, which a remote desktop needs: it is a video of a screen.
        "capabilities": ["core", "vision"],
        "outputDir": str(folder),
        "imageResponses": "omit" if unseen else "allow",
        "network": {"blockedOrigins": blocked(settings)},
    }


def prune(root: Path, days: int, now: datetime) -> None:
    """Delete every run older than ``days``, and every session folder left empty."""
    cutoff = now - timedelta(days=days)
    for session in root.iterdir() if root.is_dir() else []:
        old = [
            run
            for run in (session.iterdir() if session.is_dir() else [])
            if (found := RUN.match(run.name)) and datetime.strptime(found[1], STAMP).replace(tzinfo=UTC) < cutoff
        ]
        # Two testers starting at once may clean up together: what the other removed is gone either way.
        for run in old:
            shutil.rmtree(run, ignore_errors=True)
        try:
            if not any(session.iterdir()):
                session.rmdir()
        # telemetry: expected — the other tester removed the folder, or a new run filled it
        except OSError:
            continue


def prepare(role: str, session_id: str, root: Path, settings: Settings, now: datetime) -> Path:
    """The run's folder, with who ran it and Playwright's settings written in it; returns the settings file."""
    if role not in settings.get_list("subagents.browser_personas"):
        raise BrowserError(f"{role!r} is not a tester with a browser")
    prune(root, settings.get_int("qa.evidence_days"), now)
    folder = run_folder(root, session_id, now)
    meta = {"role": role, "session_id": session_id, "started": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
    (folder / "run.json").write_text(json.dumps(meta), encoding="utf-8")
    config = folder / "playwright.json"
    config.write_text(json.dumps(playwright_settings(folder, role, settings)), encoding="utf-8")
    return config


def main(argv: list[str], places: Places) -> int:
    if len(argv) != 1:
        print("factory qa-browser <role>", file=sys.stderr)
        return 2
    try:
        session_id = environment.claude_session_id()  # Claude Code names the session it starts this server for
        if not session_id:
            raise BrowserError("Claude Code gave no session id (CLAUDE_CODE_SESSION_ID)")
        settings = settings_module.build(store.read_json(places.state / "public" / "settings.json"), {})
        config = prepare(argv[0], session_id, Path.home() / RUNS, settings, datetime.now(UTC))
    except (BrowserError, settings_module.SettingsError, store.StoreError, OSError) as exc:
        print(f"factory qa-browser: no browser: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    os.chdir(config.parent)
    os.execvp("npx", ["npx", "-y", PLAYWRIGHT_MCP, "--config", str(config)])
