"""A scenario as slot 0 runs it: `factory qa scenario <name>`.

A regression tester walks slot 0 at the build it answers with, which can be older than the default branch's tip: a
bundle walks the commit its merges reached. The checkout holds the tip's text, which a merge since may have rewritten,
so a walk of it could fail by construction and blame the wrong merge.
"""

from __future__ import annotations

import re
from typing import Any

from runtime.qa.scenarios import FOLDER
from runtime.shared.protocol import Refused, ok
from runtime.warden.context import Context
from runtime.warden.delivery import deploys
from runtime.warden.health import probes

_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def show(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    name = args.get("name")
    if not isinstance(name, str) or not _NAME.match(name):
        raise Refused("qa-scenario", "name a scenario by its file name without .md: factory qa scenario <name>")
    current = deploys.builds(ctx.places, probes.STAGING)["current"]
    if current is None:
        raise Refused("qa-scenario", "which build slot 0 runs is not known yet: ask the orchestrator")
    commit, repo = current["commit"], ctx.settings().get_str("repo.path")
    code, text = ctx.launcher.agent_command(["git", "-C", repo, "show", f"{commit}:{FOLDER}/{name}.md"], repo)
    if code != 0:
        raise Refused("qa-scenario", f"slot 0 runs {commit[:9]}, which has no scenario {name}")
    return ok(message=f"{name}, as slot 0 runs it at {commit[:9]}:\n\n{text.rstrip()}")
