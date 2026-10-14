"""Who writes where in the product's checkouts, beyond the protected paths.

A subagent reads and reports (`subagent-writes`): in any checkout of the product it writes only in its session's
scratch folder, `.factory/` in the worktree, and it runs only git commands that read, so no tester or critic can
reset, stash or edit the implementer's uncommitted work. The orchestrator steers and never writes product code
(`orchestrator-writes`): in its checkout it writes only to `.factory/`, and on the default branch it commits only to
finish a merge or a revert, while git holds MERGE_HEAD or REVERT_HEAD.
"""

from __future__ import annotations

import os
from pathlib import Path

from runtime.guard import git_rules, shell, writes
from runtime.guard.facts import ORCHESTRATOR, Call, Decision, Refusal, inside, resolve
from runtime.shared.paths import SCRATCH

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


def in_scratch(call: Call, path: Path) -> bool:
    return inside(Path(os.path.realpath(path)), Path(os.path.realpath(call.worktree)) / SCRATCH)


def _refused_at(call: Call, path: Path) -> Refusal | None:
    """A write to ``path`` inside a checkout of the product, outside the session's scratch folder."""
    real = Path(os.path.realpath(path))
    if in_scratch(call, real):
        return None
    if not any(inside(real, root) for root in writes.product_roots(call)):
        return None
    if call.in_subagent:
        return Refusal("subagent-writes", f"a subagent writes only in {call.worktree / SCRATCH}; report to your parent")
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
    for word, _ in writes.written(inv):
        found = _refused_at(call, resolve(word, cwd, call.home))
        if found is not None:
            return found
    return None
