"""Start a long-running factory service from its module, with the install's root on sys.path.

Run as ``/usr/bin/python3 -I <install>/runtime/system/runners/start.py <module>``; the module's ``main()`` returns the
exit status. Stdlib only.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

SERVICES = frozenset({"runtime.warden.wardend", "runtime.dashboard.server"})


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] not in SERVICES:
        print(f"start: name one of {', '.join(sorted(SERVICES))}", file=sys.stderr)
        return 2
    status = importlib.import_module(argv[0]).main()
    return status if isinstance(status, int) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
