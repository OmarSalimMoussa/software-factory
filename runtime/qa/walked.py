"""A scenario, or a qa procedure one points at (qa/<name>.md), as slot 0 runs it: `factory qa scenario <name>`.

A regression tester walks slot 0 at the build its health answer names, which can be older than the default branch's
tip: a bundle walks the commit its merges reached. The checkout holds the tip's text, which a merge since may have
rewritten, so a walk of it could fail by construction and blame the wrong merge.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

from runtime.qa.scenarios import FOLDER
from runtime.shared.protocol import Refused, ok
from runtime.slots import health
from runtime.warden.context import Context

_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def show(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    name = args.get("name")
    if not isinstance(name, str) or not _NAME.match(name):
        raise Refused("qa-scenario", "name a scenario by its file name without .md: factory qa scenario <name>")
    try:
        commit = health.build(ctx, 0, ctx.caller.session)
    except health.SlotUnread as exc:
        raise Refused("qa-scenario", f"slot 0 gives no build ({exc}): ask the orchestrator") from exc
    repo = ctx.settings().get_str("repo.path")
    for folder in (FOLDER, str(PurePosixPath(FOLDER).parent)):
        code, text = ctx.launcher.agent_command(["git", "-C", repo, "show", f"{commit}:{folder}/{name}.md"], repo)
        if code == 0:
            return ok(message=f"{name}, as slot 0 runs it at {commit[:9]}:\n\n{text.rstrip()}")
    raise Refused("qa-scenario", f"slot 0 runs {commit[:9]}, which has no scenario or qa procedure {name}")
