"""software-agent-run@<id>.service: one request as the agent user, a session's start or one short command.

Run as ``/usr/bin/python3 -I <install>/runtime/system/runners/agent_run.py <state> <id>``. Its output and exit status
are the unit's, which systemd hands to the warden. Stdlib only.
"""

from __future__ import annotations

import sys
from pathlib import Path

# `python3 -I` leaves the script's own folder off sys.path; the install's root is root-owned, so it is safe to add.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from runtime.shared import environment
from runtime.system.runners import launch_file as lf


def main(argv: list[str]) -> int:
    state, request_id = Path(argv[0]), argv[1]
    try:
        raw = lf.read(state, request_id, (lf.SESSION, lf.COMMAND, lf.MESSAGE))
        settings, _ = lf.published(state)
        inherited = environment.handed_on()
        if raw["kind"] == lf.MESSAGE:
            command, repo = lf.message_argv(raw), Path(settings.get_str("repo.path"))
            env, fds = lf.claude_env(inherited, environment.credentials_folder())
            hold, stop = settings.get_int("watch.message_seconds"), settings.get_int("watch.stop_seconds")
            return lf.run_in_terminal(command, repo, env, hold, stop, fds)
        if raw["kind"] == lf.SESSION:
            command, cwd = lf.session_argv(raw, state), lf.session_cwd(raw, settings)
        else:
            command, cwd = lf.command_argv(raw, Path.home(), state)
        env, fds = (
            lf.claude_env(inherited, environment.credentials_folder())
            if command[0] == lf.PROGRAMS["claude"]
            else (inherited, ())
        )
    # telemetry: surfaced — the refusal is `print`ed to the unit's log, and the exit says it was refused
    except lf.BadRequest as exc:
        print(f"agent-run: refused: {exc}", flush=True)
        return lf.REFUSED
    return lf.run(command, cwd, env, settings.get_int("factory.command_seconds"), fds)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
