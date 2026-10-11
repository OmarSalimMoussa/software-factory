"""Marks the agents' checkout as a workspace Claude Code trusts, as the agent user, in its own ~/.claude.json.

Run by the install as ``/usr/bin/python3 -I <install>/runtime/system/runners/trust.py <the agent's .claude.json>
<checkout>``. Claude Code refuses to start a background session in a folder nobody accepted its trust prompt for, and
no one is at the agent's keyboard to accept it: this sets the answer Claude Code records, and keeps the rest of the
file as it is. Stdlib only.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path


def trust(config: Path, checkout: str) -> None:
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    if not isinstance(data, dict) or not isinstance(data.setdefault("projects", {}), dict):
        raise SystemExit(f"{config}: not Claude Code's settings")
    data["projects"].setdefault(checkout, {})["hasTrustDialogAccepted"] = True
    fd, tmp = tempfile.mkstemp(dir=config.parent, prefix=f".{config.name}.")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, config)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: trust.py <the agent's .claude.json> <checkout>", file=sys.stderr)
        return 2
    trust(Path(argv[0]), argv[1])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
