"""The pass record: `factory check` records the git tree it passed, and the merge hook skips the checks for that tree.

A pass is keyed by the tree id, so it can never cover other content: a merge commit whose tree equals a checked
branch's tree holds nothing the checks did not see. The record is one line per pass, ``<tree> <branch> <time>``, in
``<git common dir>/factory-check-passes``: shared by every worktree of the clone, never committed, and a protected
path the guard refuses any agent to write. Only `factory check` writes it.

Standard library only, so the git hooks run it as a script: ``python3 -I passes.py check <tree>`` exits 0 when the
tree passed and 1 when it did not.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

RECORD = "factory-check-passes"


def record_path(root: Path) -> Path:
    common = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return Path(common) / RECORD


def passed(root: Path, tree: str) -> bool:
    path = record_path(root)
    if not path.exists():
        return False
    return any(line.split(" ", 1)[0] == tree for line in path.read_text(encoding="utf-8").splitlines())


def record(root: Path, tree: str, branch: str, at: str) -> None:
    with record_path(root).open("a", encoding="utf-8") as handle:
        handle.write(f"{tree} {branch} {at}\n")


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "check":
        return 0 if passed(Path.cwd(), argv[1]) else 1
    print("usage: passes.py check <tree>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
