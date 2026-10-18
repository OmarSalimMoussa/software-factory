"""Slot 0 at a commit: a check of the default branch may walk an older commit than the tip, such as a bundle's or one
between its suspects, and slot 0 then builds it from a worktree of its own.

`factory slot up --at <commit>` makes that worktree afresh at a commit of the default branch, detached, and publishes
the commit; the stack runner runs every slot 0 verb there while it is published (launch_file.checkout_for). A plain
`factory slot up` withdraws it first, so slot 0 builds the main checkout's tip again. The worktree is removed and
added again each time: a folder there that is not that worktree is refused, never built, and git never reaches past
it into the main checkout.
"""

from __future__ import annotations

from pathlib import Path

from runtime.queue import formats
from runtime.shared import store
from runtime.shared.protocol import Refused
from runtime.warden import public
from runtime.warden.context import Context
from runtime.warden.sessions import session_settings


def place(ctx: Context, at: object) -> str:
    """Point slot 0 at the commit ``at`` names, or back at the tip when it names none; the commit, or ""."""
    record = public.staging_at_path(ctx.places)
    if not at:
        record.unlink(missing_ok=True)
        return ""
    if not isinstance(at, str) or not formats.SHA.match(at):
        raise Refused("args", "--at names a commit of the default branch by its sha")
    settings = ctx.settings()
    repo, default = settings.get_str("repo.path"), settings.get_str("repo.default_branch")
    missing, commit = _git(ctx, repo, "rev-parse", "--verify", "--quiet", f"{at}^{{commit}}", allowed={0, 1})
    if missing:
        raise Refused("args", f"the checkout has no commit {at}")
    if _git(ctx, repo, "merge-base", "--is-ancestor", commit, f"refs/heads/{default}", allowed={0, 1})[0]:
        raise Refused("args", f"{at} is not on {default}: slot 0 runs only what the default branch holds")
    where = session_settings.staging_at(Path(repo))
    record.unlink(missing_ok=True)
    _git(ctx, repo, "worktree", "remove", "--force", str(where), allowed={0, 128})  # 128: no worktree was there
    _git(ctx, repo, "worktree", "prune")
    _git(ctx, repo, "worktree", "add", "--force", "--detach", str(where), commit)
    store.write_json(record, {"commit": commit})
    return commit


def _git(ctx: Context, repo: str, *args: str, allowed: set[int] | None = None) -> tuple[int, str]:
    """git's exit status, one of ``allowed`` (0 alone unless named), and what it printed; else refused."""
    code, out = ctx.launcher.agent_command(["git", "-C", repo, *args], repo)
    if code not in (allowed or {0}):
        raise Refused("git", f"git {args[0]} {args[1]} failed: {out.strip()[-200:]}")
    return code, out.strip()
