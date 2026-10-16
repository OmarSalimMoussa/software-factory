"""The drills' worktrees in the product checkout: a and b, which the drills name, and one made for each drill session.

Claude Code's sandbox lays an empty file over each protected file a worktree lacks for one command, then deletes it,
never asking whether another process took that file for the worktree's own meanwhile; that process's sandbox then
cannot start ("Can't find source path"). So no two sessions share a folder: each agent has a worktree of its own,
and each drill session gets one too, so the drills prove the walls as the agents meet them.

Each is on a branch of its own name, made from HEAD; sessions start side by side, and git adds one worktree at a time.
None is removed while drills run: each session's settings deny reads of the others', and a sandbox cannot start once
a folder it names is gone. A sweep before and after the drills removes every drill folder, then has git forget them
(a folder a sandbox left is no worktree git lists), and their branches: this install's or a stopped one's, never an
agent's. Every drill names what it makes with NAME: its worktrees and branches, units, sessions and scratch checkout.
"""

from __future__ import annotations

import re
import threading
import uuid
from pathlib import Path

from runtime.shared.paths import AGENT_USER
from runtime.system.host import Host
from runtime.warden.sessions import session_settings

NAME = "software-drill"
PREFIX = f"{NAME}-"
_OURS = re.compile(rf"{re.escape(PREFIX)}(a|b|[0-9a-f]{{8}})")


class Worktrees:
    def __init__(self, host: Host, checkout: Path) -> None:
        self.host, self.checkout = host, checkout
        self.root = session_settings.worktrees(checkout)
        self.a, self.b = self.root / f"{PREFIX}a", self.root / f"{PREFIX}b"
        self._lock = threading.Lock()

    def open(self) -> None:
        self.sweep()
        for path in (self.a, self.b):
            self._add(path)

    def sweep(self) -> None:
        left = sorted(str(path) for path in self.root.glob(f"{PREFIX}*") if _OURS.fullmatch(path.name))
        if left:
            self.host.run(["rm", "-rf", "--", *left], user=AGENT_USER)
        self._git("worktree", "prune")
        for branch in self._git("branch", "--list", "--format=%(refname:lstrip=2)", f"{PREFIX}*").split():
            if _OURS.fullmatch(branch):
                self._git("branch", "-D", branch)

    def own(self) -> Path:
        path = self.root / f"{PREFIX}{uuid.uuid4().hex[:8]}"
        with self._lock:
            self._add(path)
        return path

    def _add(self, path: Path) -> None:
        self._git("worktree", "add", "-q", "-b", path.name, str(path), "HEAD")

    def _git(self, *argv: str) -> str:
        return self.host.run(["git", "-C", str(self.checkout), *argv], user=AGENT_USER)
