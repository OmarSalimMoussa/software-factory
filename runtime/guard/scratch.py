"""Who writes where in the product's checkouts, beyond the protected paths.

A subagent reads and reports (`subagent-writes`): in any checkout of the product it writes only in its session's
scratch folder, `.factory/` in the worktree, a tester only in its own folder there, and it runs only git commands that
read, so no tester or critic can reset, stash or edit the implementer's uncommitted work. The orchestrator steers and
never writes product code (`orchestrator-writes`): in its checkout it writes only to `.factory/`, and on the default
branch it commits only to finish a merge or a revert, while git holds MERGE_HEAD or REVERT_HEAD; a revert's commit
keeps git's line naming the commit it takes back.
"""

from __future__ import annotations

import os
from pathlib import Path

from runtime.guard import git_rules, shell, writes
from runtime.guard.facts import ORCHESTRATOR, Call, Decision, Refusal, inside, resolve
from runtime.shared.models import TESTER
from runtime.shared.paths import SCRATCH, scratch_of

# git commands that only read, beyond git_rules.READS (which an unpermitted session may also run).
_READS = git_rules.READS | frozenset(
    {"grep", "merge-base", "cat-file", "describe", "shortlog", "show-ref", "for-each-ref", "ls-tree", "rev-list",
     "name-rev", "whatchanged", "count-objects", "var", "help", "version"}
)  # fmt: skip
_FINISHING = ("MERGE_HEAD", "REVERT_HEAD")


def _applies(call: Call) -> bool:
    return call.in_subagent or call.role == ORCHESTRATOR


def _rule(call: Call) -> str:
    return "subagent-writes" if call.in_subagent else "orchestrator-writes"


def _folder(call: Call) -> str:
    return scratch_of(call.agent_id) if call.in_subagent and call.agent_type.startswith(TESTER) else SCRATCH


def in_scratch(call: Call, path: Path) -> bool:
    return inside(Path(os.path.realpath(path)), Path(os.path.realpath(call.worktree)) / _folder(call))


def _refused_at(call: Call, path: Path) -> Refusal | None:
    """A write to ``path`` inside a checkout of the product, outside the session's scratch folder."""
    real = Path(os.path.realpath(path))
    if in_scratch(call, real):
        return None
    if not any(inside(real, root) for root in writes.product_roots(call)):
        return None
    if call.in_subagent:
        where = call.worktree / _folder(call)
        return Refusal("subagent-writes", f"a subagent writes only in {where}; report to your parent")
    return Refusal("orchestrator-writes", f"you never write product code; write only in {call.worktree / SCRATCH}")


def check_edit(call: Call) -> Decision:
    raw = str(call.tool_input.get("file_path") or call.tool_input.get("notebook_path") or "")
    if not raw or not _applies(call) or call.off(_rule(call)):
        return None
    return _refused_at(call, resolve(raw, call.cwd, call.home))


def check_shell(call: Call, inv: shell.Invocation, cwd: Path) -> Decision:
    if not _applies(call) or call.off(_rule(call)):
        return None
    args = git_rules.git_args(inv)
    sub = git_rules.subcommand(args)[0] if args is not None else ""
    if call.in_subagent and args is not None and sub and sub not in _READS:
        return Refusal("subagent-writes", f"a subagent runs only git commands that read, not git {sub}")
    if not call.in_subagent and sub == "commit" and not any((call.worktree / ".git" / h).exists() for h in _FINISHING):
        default = call.settings.get_str("repo.default_branch")
        return Refusal("orchestrator-writes", f"you commit on {default} only to finish a merge or a revert")
    unnamed = _unnamed_revert(call, args) if not call.in_subagent and sub == "commit" and args is not None else None
    if unnamed is not None:
        return unnamed
    for word, _ in writes.written(inv):
        found = _refused_at(call, resolve(word, cwd, call.home))
        if found is not None:
            return found
    return None


def _unnamed_revert(call: Call, args: list[str]) -> Refusal | None:
    """A revert's commit keeps git's own line naming the merge it takes back: the prod push gate and the Done page
    find what a revert took back by that line alone."""
    head = call.worktree / ".git" / "REVERT_HEAD"
    if not head.exists():
        return None
    line = f"This reverts commit {head.read_text(encoding='utf-8').strip()}"
    if "--no-edit" in args or any(line in arg for arg in args):
        return None
    told = f"a revert's commit keeps git's line naming what it takes back: add -m '{line}.' after your own -m"
    return Refusal("orchestrator-writes", told)
