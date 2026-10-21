"""A scenario, the scenarios' README, or a qa procedure one points at (qa/<name>.md), as slot 0 runs it:
`factory qa scenario <name>`; each standing scenario at the default branch's tip, which a bundle reads; and the
scenario at the head a tester marks its steps on, whose numbers the marks follow.

A regression tester walks slot 0 at the build its health answer names, which can be older than the default branch's
tip: a bundle walks the commit its merges reached. The checkout holds the tip's text, which a merge since may have
rewritten, so a walk of it could fail by construction and blame the wrong merge.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any

from runtime.qa.scenarios import FOLDER, Scenario, named, parse
from runtime.shared.paths import scratch_of
from runtime.shared.protocol import Refused, ok
from runtime.slots import health
from runtime.warden.context import Context
from runtime.warden.sessions import doing, registry

_NAME = re.compile(r"^(?:[a-z0-9][a-z0-9-]*|README)$")


def show(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    name = args.get("name")
    if not isinstance(name, str) or not _NAME.match(name):
        raise Refused("qa-scenario", "name a scenario by its file name without .md: factory qa scenario <name>")
    try:
        commit = health.build(ctx, 0, ctx.caller.session)
    except health.SlotUnread as exc:
        raise Refused("qa-scenario", f"slot 0 gives no build ({exc}): ask the orchestrator") from exc
    repo = ctx.settings().get_str("repo.path")
    session = registry.read(ctx.places).get(ctx.caller.session)
    # A tester's first call: it names the one folder the guard lets the tester write in.
    own = (
        {"folder": str(Path(session.worktree) / scratch_of(ctx.caller.agent_id))}
        if session and ctx.caller.agent_id
        else {}
    )
    for folder in (FOLDER, str(PurePosixPath(FOLDER).parent)):
        code, text = ctx.launcher.agent_command(["git", "-C", repo, "show", f"{commit}:{folder}/{name}.md"], repo)
        if code == 0:
            if ctx.caller.agent_id and named(f"{folder}/{name}.md"):
                with ctx.lock:
                    doing.note_scenario(ctx.places, ctx.caller.agent_id, name)
            return ok(message=f"{name}, as slot 0 runs it at {commit[:9]}:\n\n{text.rstrip()}", **own)
    raise Refused("qa-scenario", f"slot 0 runs {commit[:9]}, which has no scenario or qa procedure {name}")


def texts(ctx: Context, commit: str, paths: str) -> dict[str, str]:
    """The text of each file at ``commit`` the pathspec ``paths`` names, by its path: none when it names none."""
    repo = ctx.settings().get_str("repo.path")
    argv = ["git", "-C", repo, "grep", "-n", "--full-name", "-I", "-e", "", commit, "--", paths]
    code, out = ctx.launcher.agent_command(argv, repo)
    if code == 1 and not out.strip():  # git grep found no line: no such file
        return {}
    if code != 0:
        raise Refused("git", f"git grep failed: {out.strip()[-200:]}")
    lines: dict[str, list[str]] = {}
    for row in out.splitlines():
        rel, _, numbered = row.removeprefix(f"{commit}:").partition(":")
        lines.setdefault(rel, []).append(numbered.partition(":")[2])
    return {rel: "\n".join(said) + "\n" for rel, said in lines.items()}


def standing(ctx: Context, tip: str, max_steps: int, max_items: int) -> dict[str, Scenario]:
    """Each scenario at ``tip`` a tester may walk now (none waits for what it needs), by name."""
    found, problems = {}, []
    for rel, text in texts(ctx, tip, f"{FOLDER}/*.md").items():
        name = named(rel)
        if name is None:
            continue
        scenario, said = parse(name, text, max_steps, max_items)
        problems += said
        if scenario is not None and scenario.runs != "waiting":
            found[name] = scenario
    if problems:
        raise Refused("scenarios", "; ".join(problems))
    return found
