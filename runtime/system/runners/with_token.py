"""The agents' Claude daemon, signed in with the token systemd loaded for its unit.

Run as ``/usr/bin/python3 -I <install>/runtime/system/runners/with_token.py <claude> daemon run``. Claude Code's daemon
hands each session it starts a snapshot of its stored sign-in, never a token it was given on a pipe or a descriptor:
so the token goes into the agent user's stored sign-in first, and the daemon starts after. The token never appears
on a command line or in an environment. Stdlib only.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# `python3 -I` leaves the script's own folder off sys.path; the install's root is root-owned, so it is safe to add.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from runtime.shared import environment, store
from runtime.shared.paths import stored_signin
from runtime.system import layout
from runtime.system.runners import launch_file as lf

# A token from `claude setup-token` lasts a year, with no refresh token (Claude Code reads an empty one as signed out).
SETUP_TOKEN_DAYS = 365
SCOPES = ["user:inference"]  # all a setup-token may do: model requests


def signin(existing: str, token: str, now: float) -> str:
    """The stored sign-in holding ``token``, keeping whatever else the file held."""
    try:
        held = json.loads(existing) if existing.strip() else {}
    # telemetry: expected — a file Claude Code was cut off writing is replaced whole
    except ValueError:
        held = {}
    expires = int((now + SETUP_TOKEN_DAYS * 86400) * 1000)
    oauth = {"accessToken": token, "refreshToken": None, "expiresAt": expires, "scopes": SCOPES}
    return json.dumps({**(held if isinstance(held, dict) else {}), "claudeAiOauth": oauth}) + "\n"


def main(argv: list[str]) -> int:
    path = stored_signin(layout.AGENT_HOME)
    try:
        token = lf.unit_token(environment.credentials_folder())
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        store.write_text(path, signin(existing, token, time.time()), mode=0o600)
    except (lf.BadRequest, OSError) as exc:
        print(f"with-token: the daemon was not signed in: {exc}", file=sys.stderr, flush=True)
        return lf.REFUSED
    # argv[0] stays the claude path itself: the warden's vitals know the daemon by it.
    os.execve(argv[0], argv, environment.handed_on())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
