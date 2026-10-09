"""ExecStopPost of both template units, run as root: write how the request ended where the warden reads it.

systemd sets EXIT_CODE (exited, killed, dumped) and EXIT_STATUS (the number, or the signal's name). The warden reads
a number as the exit status and anything else as what ended the run. Stdlib only.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    status = os.environ.get("EXIT_STATUS") or os.environ.get("SERVICE_RESULT") or "unknown"
    path = Path(argv[0])
    path.write_text(status + "\n", encoding="utf-8")
    path.chmod(0o644)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
