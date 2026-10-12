"""The agents' Claude daemon, started with the sign-in token systemd loaded for its unit.

Run as ``/usr/bin/python3 -I <install>/runtime/system/runners/with_token.py <claude> daemon run``. The token never
appears on a command line or in an environment: claude reads it from a pipe it inherits. Stdlib only.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# `python3 -I` leaves the script's own folder off sys.path; the install's root is root-owned, so it is safe to add.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from runtime.shared import environment
from runtime.system.runners import launch_file as lf


def main(argv: list[str]) -> int:
    try:
        # The token's pipe is inheritable, so it survives the exec.
        env, _ = lf.claude_env(environment.handed_on(), environment.credentials_folder())
    except lf.BadRequest as exc:
        print(f"with-token: {exc}", file=sys.stderr, flush=True)
        return lf.REFUSED
    # argv[0] stays the claude path itself: the warden's vitals know the daemon by it.
    os.execve(argv[0], argv, env)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
