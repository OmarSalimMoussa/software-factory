"""`prod-frozen`: nothing an agent runs reaches prod. CI deploys staging from the default branch, and only the owner's
push to prod starts the prod workflow; a deploy runs on the server, through a key only CI holds. So no agent starts a
workflow, or pushes a tag or another branch."""

from __future__ import annotations

from pathlib import Path

from runtime.guard import git_rules, nested, shell
from runtime.guard.facts import Call, Decision, Refusal

_INTERPRETERS = nested.SHELLS | {"python", "python3", "node", "perl", "ruby"}
_WORKFLOW_STARTS = frozenset({("workflow", "run"), ("workflow", "enable"), ("workflow", "disable"), ("run", "rerun")})
_SHIPS_ALL = frozenset({"--tags", "--follow-tags", "--mirror", "--all"})


def script_of(inv: shell.Invocation) -> str:
    """The file an invocation runs: a program named by its path, or an interpreter's script."""
    if "/" in (inv.argv[0] if inv.argv else ""):
        return inv.argv[0]
    if inv.program in _INTERPRETERS or inv.program.startswith("python"):
        words = [a for a in inv.argv[1:] if not a.startswith("-")]
        return words[0] if words else ""
    return ""


def check(call: Call, inv: shell.Invocation, cwd: Path) -> Decision:
    if call.off("prod-frozen") or not inv.argv:
        return None
    words = inv.argv[1:]
    dispatch = words[:1] == ["api"] and any("dispatches" in a for a in words) and git_rules.api_writes(words)
    if inv.program == "gh" and (tuple(words[:2]) in _WORKFLOW_STARTS or dispatch):
        return Refusal("prod-frozen", "workflows start only from CI and the owner, who pushes from Home")
    args = git_rules.git_args(inv)
    if args is not None and git_rules.subcommand(args)[0] == "push" and _ships_elsewhere(call, args):
        return Refusal("prod-frozen", "tags and other branches never leave this machine; only the owner promotes")
    return None


def _ships_elsewhere(call: Call, args: list[str]) -> bool:
    """A push of a tag, of everything, or to any branch but the default one."""
    _, rest = git_rules.subcommand(args)
    if _SHIPS_ALL & set(rest):
        return True
    refspecs = [a for a in rest if not a.startswith("-")][1:]
    default = call.settings.get_str("repo.default_branch")
    for spec in refspecs:
        dest = spec.lstrip("+").rsplit(":", 1)[-1]
        if "refs/tags/" in spec or dest not in {default, f"refs/heads/{default}"}:
            return True
    return False
