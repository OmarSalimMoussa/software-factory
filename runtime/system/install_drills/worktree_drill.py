"""The drill that a slot's containers can read what an agent writes. A container reads the worktree through its bind
mounts as a user outside the agent's group, so a file a session writes must be readable by others: the agents'
daemon's umask decides that, and the drill runs inside its walls."""

from __future__ import annotations

import shlex
import uuid

from runtime.system.install_drills.evidence import Result, Rig, first_line

READABLE = "644"


def worktree_readable(rig: Rig) -> Result:
    name = "a slot's containers can read what an agent writes"
    probe = shlex.quote(str(rig.worktree_a / f".drill-readable-{uuid.uuid4().hex[:8]}"))
    ran = rig.as_daemon(["sh", "-c", f": > {probe} && stat -c %a {probe} && rm -f {probe}"])
    mode = first_line(ran.out)
    return Result(name, ran.code == 0 and mode == READABLE, f"a new file's mode: {mode or 'none'}, exit {ran.code}")
