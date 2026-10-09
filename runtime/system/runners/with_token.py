"""The agents' Claude daemon, started with the sign-in token systemd loaded for its unit.

Run as ``/usr/bin/python3 -I <install>/runtime/system/runners/with_token.py <claude> daemon run``. The token never
appears on a command line; the daemon's sessions scrub it from every shell they start. Stdlib only.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from runtime.system.runners import launch_file as lf  # noqa: E402 - the path line above must run first


def main(argv: list[str]) -> int:
    try:
        env = lf.claude_env(dict(os.environ))
    except lf.BadRequest as exc:
        print(f"with-token: {exc}", file=sys.stderr, flush=True)
        return lf.REFUSED
    # argv[0] stays the claude path itself: the warden's vitals know the daemon by it.
    os.execve(argv[0], argv, env)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
