"""software-agent-run@<id>.service: one request as the agent user, a session's start or one short command.

Run as ``/usr/bin/python3 -I <install>/runtime/system/runners/agent_run.py <state> <id>``. Its output and exit status
are the unit's, which systemd hands to the warden. Stdlib only.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# `python3 -I` leaves the script's own folder off sys.path; the install's root is root-owned, so it is safe to add.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from runtime.system.runners import launch_file as lf  # noqa: E402 - the path line above must run first


def main(argv: list[str]) -> int:
    state, request_id = Path(argv[0]), argv[1]
    try:
        raw = lf.read(state, request_id, (lf.SESSION, lf.COMMAND))
        settings, _ = lf.published(state)
        environ = dict(os.environ)
        if raw["kind"] == lf.SESSION:
            command, cwd = lf.session_argv(raw, state), Path(settings.get_str("repo.path"))
        else:
            command, cwd = lf.command_argv(raw, Path.home())
        env = lf.claude_env(environ) if command[0] == lf.PROGRAMS["claude"] else environ
    except lf.BadRequest as exc:
        print(f"agent-run: refused: {exc}", flush=True)
        return lf.REFUSED
    return lf.run(command, cwd, env, settings.get_int("factory.command_seconds"))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
