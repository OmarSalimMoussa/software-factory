"""The warden's own mirror of the product's repo, and GitHub as a mirror of the default branch.

The warden's bare mirror fetches the agents' checkout, which needs no network, and GitHub's default branch over SSH
with the deploy key, which no agent can read. The Done refresh brings GitHub's branch up to the checkout's tip: a
fast-forward of whatever the tip holds, never a push over commits GitHub took on its own. It also asks the
orchestrator to check a tip that waits on nothing else. The owner's push to prod, and back, is prod_push.py's; it
shares the one lock, so one push or fetch of the checkout runs at a time.
"""

from __future__ import annotations

import logging
import re
import shlex
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from runtime.queue import turn
from runtime.shared import environment
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused
from runtime.warden import checks, inbox
from runtime.warden.context import Context
from runtime.warden.delivery import deploys
from runtime.warden.health import probes

log = logging.getLogger(__name__)

# What `git revert` writes into the message of the commit that takes a merge back.
REVERTS = re.compile(r"This reverts commit ([0-9a-f]{40})")
# One push at a time, and one fetch of the checkout: the check that none runs and the start happen together. The tick
# takes it inside ctx.lock and the queue's lock, so whoever holds it takes neither and never waits on the network.
pushing = threading.Lock()
_ASKED: dict[str, str] = {}  # each state folder's tip the orchestrator was last asked to check
_UNMIRRORED: dict[str, str] = {}  # each state folder's reason GitHub is not mirrored, logged once while it lasts


def deploy_key(places: Places) -> Path:
    return places.state / "secrets" / "deploy.key"


def fetch_checkout(ctx: Context, git: Git) -> str:
    """Fetch the agents' checkout's default branch into the mirror without the network; the branch's tip. The caller
    holds pushing."""
    settings = ctx.settings()
    checkout, branch = settings.get_str("repo.path"), settings.get_str("repo.default_branch")
    # The checkout belongs to the agent, so git must be told it may read it; a fetch reads it only through
    # upload-pack, the side of git built to serve untrusted repositories. The setting goes on upload-pack's own
    # command line (run through a shell): git drops a fetch's -c options when it starts a local upload-pack, which
    # checks the owner of the git folder, not of the checkout.
    trust = shlex.quote(f"safe.directory={Path(checkout) / '.git'}")
    upload_pack = f"git -c {trust} upload-pack"
    git.run("fetch", "--no-tags", "--quiet", "--upload-pack", upload_pack, checkout,
            f"+refs/heads/{branch}:refs/checkout/{branch}")  # fmt: skip
    return git.run("rev-parse", f"refs/checkout/{branch}").strip()


def mirror(ctx: Context, git: Git, tip: str) -> None:
    """Bring GitHub's default branch up to ``tip``, the checkout's: a fast-forward, whatever paths it changes. A
    branch that took commits on GitHub itself stays as it is. That, or GitHub not answering, is logged once while it
    lasts; the refresh goes on either way."""
    settings = ctx.settings()
    branch, url = settings.get_str("repo.default_branch"), f"git@github.com:{settings.get_str('repo.github')}.git"
    key = str(ctx.places.state)
    try:
        git.run("fetch", "--no-tags", "--quiet", url, f"+refs/heads/{branch}:refs/github/{branch}")
        remote = git.run("rev-parse", f"refs/github/{branch}").strip()
        if not git.is_ancestor(remote, tip):
            raise Refused("diverged", f"{branch} on GitHub has commits the checkout lacks, up to {remote[:12]}: "
                                      "merge them into the checkout, or take them off GitHub")  # fmt: skip
        if remote != tip:
            git.run("push", "--quiet", url, f"{tip}:refs/heads/{branch}")
            git.run("update-ref", f"refs/github/{branch}", tip)
    except Refused as exc:
        if _UNMIRRORED.get(key) != exc.reason:
            log.warning("warden: GitHub's %s is not mirrored (%s): %s", branch, exc.rule, exc.reason)
        _UNMIRRORED[key] = exc.reason
        return
    _UNMIRRORED.pop(key, None)


def ask_for_check(ctx: Context, git: Git, tip: str) -> None:
    """Ask the orchestrator, once a tip, to check the default branch's ``tip`` when that is all it waits on: no turn
    held, and every merge since prod's build cleared to ship."""
    from runtime.warden.delivery.prod_push import unchecked_merges  # prod_push imports this module

    prod = deploys.builds(ctx.places, probes.PROD)["current"]
    if prod is None or not git.knows(prod["commit"]) or turn.holder(ctx.places) is not None:
        return
    base = git.run("rev-parse", f"{prod['commit']}^{{commit}}").strip()
    if tip == base or unchecked_merges(ctx, git, base, tip):
        return
    if checks.passed(ctx.places, git.run("rev-parse", f"{tip}^{{tree}}").strip()):
        return
    if _ASKED.get(str(ctx.places.state)) != tip:
        _ASKED[str(ctx.places.state)] = tip
        ctx.post_event(inbox.CHECK_DUE, {"commit": tip})


def includes(ctx: Context) -> Callable[[str, str], bool | None]:
    """Whether a build includes a merge, asked of the warden's own mirror, which fetches the agents' checkout once
    when either commit is not in it yet. None for a commit the checkout's branch does not hold either; such a build
    moves no task."""
    state: dict[str, Any] = {"git": None, "fetched": False}

    def check(build: str, merge: str) -> bool | None:
        git = state["git"] = state["git"] or Git(ctx)
        if not (git.knows(build) and git.knows(merge)) and not state["fetched"]:
            state["fetched"] = True
            with pushing:
                fetch_checkout(ctx, git)
        if not (git.knows(build) and git.knows(merge)):
            return None
        return git.is_ancestor(merge, build)

    return check


class Git:
    """git in the warden's own bare mirror, with the deploy key for GitHub and no prompt that could hang."""

    def __init__(self, ctx: Context) -> None:
        key = deploy_key(ctx.places)
        if not key.is_file():
            raise Refused("push-failed", f"there is no deploy key at {key}: reinstall to write it")
        self.mirror = ctx.places.mirror
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

    def knows(self, commit: str) -> bool:
        return self._exec(["cat-file", "-e", f"{commit}^{{commit}}"], cwd=self.mirror).returncode == 0

    def is_ancestor(self, older: str, newer: str) -> bool:
        done = self._exec(["merge-base", "--is-ancestor", older, newer], cwd=self.mirror)
        if done.returncode not in (0, 1):
            raise Refused("push-failed", f"git merge-base failed: {done.stderr.strip()[-300:]}")
        return done.returncode == 0


def _verb(argv: list[str]) -> str:
    return next(a for a in argv if not a.startswith("-") and "=" not in a)
