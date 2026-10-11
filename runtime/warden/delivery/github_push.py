"""The orchestrator's push of its checked commit to the default branch.

The warden pushes from its own mirror with the deploy key, which no agent can read. It fetches the checked commit
from the agent's checkout and refuses a range that touches a path only the owner's hand may change (plan section 1).
The owner's push to prod, and back, is prod_push.py's; the two share one lock, so one push runs at a time.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from runtime.guard.writes import UnknownProtectedPaths, is_protected, protected_paths
from runtime.queue.book import Queue
from runtime.queue.turn import merges
from runtime.shared import environment
from runtime.shared.models import ON_STAGING
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused, ok
from runtime.shared.settings import Settings
from runtime.warden import checks
from runtime.warden.context import Context
from runtime.warden.delivery import deploys, pins
from runtime.warden.health import probes

FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
# What `git revert` writes into the message of the commit that takes a merge back.
_REVERTS = re.compile(r"This reverts commit ([0-9a-f]{40})")
# A task whose merge passed its check of the default branch. A closed task's merge ships only on a saved check of
# that merge that passed: a task may be closed before its check.
SHIPPABLE = frozenset({"checked", ON_STAGING, "promoted"})
# One push at a time, of either kind: the check that none runs and the start happen together.
pushing = threading.Lock()


def mirror_path(places: Places) -> Path:
    return places.state / "mirror.git"


def deploy_key(places: Places) -> Path:
    return places.state / "secrets" / "deploy.key"


def protected_prefixes(settings: Settings) -> tuple[str, ...]:
    """The paths only the owner's hand changes: the guard's own list, so a push refuses what it does. While the main
    checkout's factory.toml cannot be read the list is unknown, and no push goes."""
    try:
        return protected_paths(settings)
    except UnknownProtectedPaths as exc:
        raise Refused("protected-path", f"{exc}: no push until the owner fixes it") from exc


def push_checked(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """Push the default branch's newest shippable commit, or the one ``sha`` names, from the agent's checkout to
    GitHub. Shippable: its tree passed factory check, and every merge it adds to GitHub's branch is one whose check
    passed (its task checked or further), or was reverted within the range."""
    sha = args.get("sha")
    if sha is not None and (not isinstance(sha, str) or not FULL_SHA.match(sha)):
        raise Refused("push", "name the commit by its full 40-character sha, or name none to push the newest")
    settings = ctx.settings()
    checkout, branch = settings.get_str("repo.path"), settings.get_str("repo.default_branch")
    url = f"git@github.com:{settings.get_str('repo.github')}.git"
    prefixes = protected_prefixes(settings)
    with pushing:
        git = Git(ctx)
        git.run("fetch", "--no-tags", "--quiet", url, f"+refs/heads/{branch}:refs/github/{branch}")
        # The checkout belongs to the agent, so git must be told it may read it; a fetch reads it only through
        # upload-pack, the side of git built to serve untrusted repositories. A checked commit need not be a branch
        # tip, so the checkout must hand over any commit it has. Both settings go on upload-pack's own command line
        # (run through a shell): git drops a fetch's -c options when it starts a local upload-pack, which checks the
        # owner of the git folder, not of the checkout.
        trust = shlex.quote(f"safe.directory={Path(checkout) / '.git'}")
        upload_pack = f"git -c {trust} -c uploadpack.allowAnySHA1InWant=true upload-pack"
        git.run("fetch", "--no-tags", "--quiet", "--upload-pack", upload_pack, checkout,
                f"+refs/heads/{branch}:refs/checkout/{branch}", *([sha] if sha else []))  # fmt: skip
        remote = git.run("rev-parse", f"refs/github/{branch}").strip()
        if sha is None:
            sha = _newest_shippable(ctx, git, remote, f"refs/checkout/{branch}")
        if not git.is_ancestor(sha, f"refs/checkout/{branch}"):
            raise Refused("push", f"{sha[:12]} is not on {branch} in the checkout: merge it, check it, then push")
        if git.is_ancestor(sha, remote):
            return ok(sha=sha, commits=0, message=f"GitHub's {branch} already holds {sha[:12]}")
        if not git.is_ancestor(remote, sha):
            raise Refused("push", f"{branch} on GitHub has commits {sha[:12]} lacks: merge them, check, then push")
        unchecked = _unchecked_merges(ctx, git, remote, sha)
        if unchecked:
            raise Refused("unchecked", f"{sha[:12]} would ship {', '.join(unchecked)}, whose check of the default "
                                       "branch has not passed: check it first, or revert it")  # fmt: skip
        owners = pins.allowed(git.run, ctx.places, remote, sha)
        bad = sorted({p for p in git.touched(remote, sha) if is_protected(p, prefixes)} - owners)
        if bad:
            raise Refused("protected-path", f"the range touches {', '.join(bad[:5])}, which only the owner changes: "
                                            "revert those changes, or ask the owner")  # fmt: skip
        tree = git.run("rev-parse", f"{sha}^{{tree}}").strip()
        if not checks.passed(ctx.places, tree) and not pins.tree_passes(git.run, ctx.places, sha):
            raise Refused("unchecked", f"{sha[:12]} has not passed factory check: run factory check on {branch}, "
                                       "then push")  # fmt: skip
        count = int(git.run("rev-list", "--count", f"{remote}..{sha}"))
        git.run("push", "--quiet", url, f"{sha}:refs/heads/{branch}")
    deploys.expect(ctx.places, probes.STAGING, sha, ctx.clock.now(), settings.get_int("watch.deploy_minutes"))
    return ok(sha=sha, commits=count, message=f"Pushed {count} commits; CI takes {sha[:12]} to staging")


def includes(ctx: Context) -> Callable[[str, str], bool | None]:
    """Whether a build includes a merge, asked of the warden's own mirror, which fetches GitHub's default branch once
    when either commit is not in it yet: a build the agents' checkout never saw (a commit made on GitHub) is known
    there. None for a commit GitHub's branch does not hold either; such a build moves no task."""
    settings = ctx.settings()
    branch = settings.get_str("repo.default_branch")
    url = f"git@github.com:{settings.get_str('repo.github')}.git"
    state: dict[str, Any] = {"git": None, "fetched": False}

    def known(git: Git, commit: str) -> bool:
        return git._exec(["cat-file", "-e", f"{commit}^{{commit}}"], cwd=git.mirror).returncode == 0

    def check(build: str, merge: str) -> bool | None:
        git = state["git"] = state["git"] or Git(ctx)
        if not (known(git, build) and known(git, merge)) and not state["fetched"]:
            state["fetched"] = True
            git.run("fetch", "--no-tags", "--quiet", url, f"+refs/heads/{branch}:refs/github/{branch}")
        if not (known(git, build) and known(git, merge)):
            return None
        return git.is_ancestor(merge, build)

    return check


def _unchecked_merges(ctx: Context, git: Git, remote: str, head: str) -> list[str]:
    """The merges on the default branch between GitHub's branch and ``head`` that are not cleared to ship: one the
    warden never saw land for a task, or whose task's check has not passed. A merge reverted within the range ships
    nothing of its own."""
    reverted = set(_REVERTS.findall(git.run("log", "--format=%B", f"{remote}..{head}")))
    found = merges(ctx.places)
    queue = Queue.of(ctx)
    passed = {c.commit for c in queue.checks()[0] if all(r.result == "pass" for r in c.rows)}
    out = []
    for merge in git.run("rev-list", "--first-parent", "--merges", f"{remote}..{head}").split():
        if merge in reverted:
            continue
        task = found.get(merge)
        status = queue.task(task).status if task else ""
        if status not in SHIPPABLE and not (status == "closed" and merge in passed):
            out.append(f"merge {merge[:12]}" + (f" ({task}, {status})" if task else " (no task's merge)"))
    return out


def _newest_shippable(ctx: Context, git: Git, remote: str, tip: str) -> str:
    """The newest first-parent commit after GitHub's branch whose tree passed and whose range ships only checked
    merges; refused, with why the tip is not shippable, when there is none."""
    candidates = git.run("rev-list", "--first-parent", f"{remote}..{tip}").split()
    for commit in candidates:
        passed = checks.passed(ctx.places, git.run("rev-parse", f"{commit}^{{tree}}").strip())
        if (passed or pins.tree_passes(git.run, ctx.places, commit)) and not _unchecked_merges(
            ctx, git, remote, commit
        ):
            return commit
    if not candidates:
        return remote
    tree = git.run("rev-parse", f"{candidates[0]}^{{tree}}").strip()
    why = ", ".join(_unchecked_merges(ctx, git, remote, candidates[0])) or (
        "its tree never passed factory check" if not checks.passed(ctx.places, tree) else "nothing"
    )
    raise Refused("unchecked", f"nothing new is ready to ship: the default branch's tip waits on {why}")


class Git:
    """git in the warden's own bare mirror, with the deploy key for GitHub and no prompt that could hang."""

    def __init__(self, ctx: Context) -> None:
        key = deploy_key(ctx.places)
        if not key.is_file():
            raise Refused("push-failed", f"there is no deploy key at {key}: reinstall to write it")
        self.mirror = mirror_path(ctx.places)
        self.timeout = ctx.settings().get_int("watch.push_wait_minutes") * 60
        self.env = {
            **environment.handed_on(),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_SSH_COMMAND": f"ssh -i {shlex.quote(str(key))} -o IdentitiesOnly=yes -o BatchMode=yes "
                               "-o StrictHostKeyChecking=yes "
                               f"-o UserKnownHostsFile={shlex.quote(str(ctx.places.github_known_hosts))}",
        }  # fmt: skip
        if not (self.mirror / "HEAD").is_file():
            self._exec(["init", "--bare", "--quiet", str(self.mirror)], cwd=self.mirror.parent)

    def _exec(self, argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        cwd.mkdir(parents=True, exist_ok=True)
        try:
            return subprocess.run(["git", *argv], cwd=cwd, env=self.env, capture_output=True, text=True,
                                  timeout=self.timeout, check=False)  # fmt: skip
        except subprocess.TimeoutExpired as exc:
            raise Refused("push-failed", f"git {_verb(argv)} did not finish in {self.timeout // 60} minutes") from exc

    def run(self, *argv: str) -> str:
        done = self._exec(list(argv), cwd=self.mirror)
        if done.returncode != 0:
            lines = done.stderr.strip().splitlines()
            said = next((line for line in lines if line.startswith(("fatal:", "error:"))), lines[-1] if lines else "")
            raise Refused("push-failed", f"git {_verb(list(argv))} failed: {(said or f'exit {done.returncode}')[:300]}")
        return done.stdout

    def is_ancestor(self, older: str, newer: str) -> bool:
        done = self._exec(["merge-base", "--is-ancestor", older, newer], cwd=self.mirror)
        if done.returncode not in (0, 1):
            raise Refused("push-failed", f"git merge-base failed: {done.stderr.strip()[-300:]}")
        return done.returncode == 0

    def touched(self, base: str, head: str) -> set[str]:
        """Every path a commit in the range changes, and every path the range changes overall (a merge's own edits).

        -z keeps every path byte for byte: a quoted name would slip past the protected prefixes.
        """
        commits = self.run("log", "--no-merges", "--no-renames", "--name-only", "--format=", "-z", f"{base}..{head}")
        overall = self.run("diff", "--no-renames", "--name-only", "-z", base, head)
        return {p.strip("\n") for p in (commits + "\0" + overall).split("\0") if p.strip("\n")}


def _verb(argv: list[str]) -> str:
    return next(a for a in argv if not a.startswith("-") and "=" not in a)
