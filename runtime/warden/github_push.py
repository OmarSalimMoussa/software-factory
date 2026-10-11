"""Pushes: the orchestrator's checked commit to the default branch, and the owner's push to prod and back.

The warden pushes from its own mirror with the deploy key, which no agent can read. It fetches the checked commit
from the agent's checkout and refuses a range that touches a path only the owner's hand may change (plan section 1).
A push to prod, or back, is an operation the dashboard follows step by step: it starts the product's prod workflow
(the factory's template, copied in) for exactly the commit staging runs, or the one prod ran before as the deploy log
has it, waits for prod to answer on it, then asks the orchestrator for a qa-prod pass. The owner's code is the only
lock on it. While a push to prod is in flight, and for watch.loop_minutes after it lands, the loop probes prod every
tick.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from runtime.guard.writes import UnknownProtectedPaths, is_protected, protected_paths
from runtime.queue.book import Queue
from runtime.queue.turn import merges
from runtime.shared import store
from runtime.shared.models import Probe
from runtime.shared.paths import PROMOTE_JOB, Places
from runtime.shared.protocol import Refused, ok
from runtime.shared.settings import Settings
from runtime.warden import checks, deploys, inbox, ops, probes
from runtime.warden.context import Context
from runtime.warden.github import GitHubError

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
# What `git revert` writes into the message of the commit that takes a merge back.
_REVERTS = re.compile(r"This reverts commit ([0-9a-f]{40})")
# A task whose merge passed its check of the default branch.
SHIPPABLE = frozenset({"checked", "on-staging", "promoted", "closed"})
_SHA = re.compile(r"^[0-9a-f]{7,40}$")
# One push at a time, of either kind: the check that none runs and the start happen together.
_pushing = threading.Lock()
_watch_lock = threading.Lock()


def mirror_path(places: Places) -> Path:
    return places.state / "mirror.git"


def deploy_key(places: Places) -> Path:
    return places.state / "secrets" / "deploy.key"


def _watch_path(places: Places) -> Path:
    return places.state / "push.json"


def protected_prefixes(settings: Settings) -> tuple[str, ...]:
    """The paths only the owner's hand changes: the guard's own list, so a push refuses what it does. While the main
    checkout's factory.toml cannot be read the list is unknown, and no push goes."""
    try:
        return protected_paths(settings)
    except UnknownProtectedPaths as exc:
        raise Refused("protected-path", f"{exc}: no push until the owner fixes it") from exc


# ---- the orchestrator's push of a checked commit ---------------------------------------------------------------


def push_checked(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """Push the default branch's newest shippable commit, or the one ``sha`` names, from the agent's checkout to
    GitHub. Shippable: its tree passed factory check, and every merge it adds to GitHub's branch is one whose check
    passed (its task checked or further), or was reverted within the range."""
    sha = args.get("sha")
    if sha is not None and (not isinstance(sha, str) or not _FULL_SHA.match(sha)):
        raise Refused("push", "name the commit by its full 40-character sha, or name none to push the newest")
    settings = ctx.settings()
    checkout, branch = settings.get_str("repo.path"), settings.get_str("repo.default_branch")
    url = f"git@github.com:{settings.get_str('repo.github')}.git"
    prefixes = protected_prefixes(settings)
    with _pushing:
        git = _Git(ctx)
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
        if remote == sha:
            return ok(sha=sha, commits=0, message=f"GitHub's {branch} already is {sha[:12]}")
        if not git.is_ancestor(remote, sha):
            raise Refused("push", f"{branch} on GitHub has commits {sha[:12]} lacks: merge them, check, then push")
        unchecked = _unchecked_merges(ctx, git, remote, sha)
        if unchecked:
            raise Refused("unchecked", f"{sha[:12]} would ship {', '.join(unchecked)}, whose check of the default "
                                       "branch has not passed: check it first, or revert it")  # fmt: skip
        bad = sorted({p for p in git.touched(remote, sha) if is_protected(p, prefixes)})
        if bad:
            raise Refused("protected-path", f"the range touches {', '.join(bad[:5])}, which only the owner changes: "
                                            "revert those changes, or ask the owner")  # fmt: skip
        tree = git.run("rev-parse", f"{sha}^{{tree}}").strip()
        if not checks.passed(ctx.places, tree):
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

    def known(git: _Git, commit: str) -> bool:
        return git._exec(["cat-file", "-e", f"{commit}^{{commit}}"], cwd=git.mirror).returncode == 0

    def check(build: str, merge: str) -> bool | None:
        git = state["git"] = state["git"] or _Git(ctx)
        if not (known(git, build) and known(git, merge)) and not state["fetched"]:
            state["fetched"] = True
            git.run("fetch", "--no-tags", "--quiet", url, f"+refs/heads/{branch}:refs/github/{branch}")
        if not (known(git, build) and known(git, merge)):
            return None
        return git.is_ancestor(merge, build)

    return check


def _unchecked_merges(ctx: Context, git: _Git, remote: str, head: str) -> list[str]:
    """The merges on the default branch between GitHub's branch and ``head`` that are not cleared to ship: one the
    warden never saw land for a task, or whose task's check has not passed. A merge reverted within the range ships
    nothing of its own."""
    reverted = set(_REVERTS.findall(git.run("log", "--format=%B", f"{remote}..{head}")))
    found = merges(ctx.places)
    out = []
    for merge in git.run("rev-list", "--first-parent", "--merges", f"{remote}..{head}").split():
        if merge in reverted:
            continue
        task = found.get(merge)
        status = Queue.of(ctx).task(task).status if task else ""
        if status not in SHIPPABLE:
            out.append(f"merge {merge[:12]}" + (f" ({task}, {status})" if task else " (no task's merge)"))
    return out


def _newest_shippable(ctx: Context, git: _Git, remote: str, tip: str) -> str:
    """The newest first-parent commit after GitHub's branch whose tree passed and whose range ships only checked
    merges; refused, with why the tip is not shippable, when there is none."""
    candidates = git.run("rev-list", "--first-parent", f"{remote}..{tip}").split()
    for commit in candidates:
        if checks.passed(ctx.places, git.run("rev-parse", f"{commit}^{{tree}}").strip()) and not _unchecked_merges(
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


class _Git:
    """git in the warden's own bare mirror, with the deploy key for GitHub and no prompt that could hang."""

    def __init__(self, ctx: Context) -> None:
        key = deploy_key(ctx.places)
        if not key.is_file():
            raise Refused("push-failed", f"there is no deploy key at {key}: reinstall to write it")
        self.mirror = mirror_path(ctx.places)
        self.timeout = ctx.settings().get_int("watch.push_wait_minutes") * 60
        self.env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_SSH_COMMAND": f"ssh -i {shlex.quote(str(key))} -o IdentitiesOnly=yes -o BatchMode=yes "
                               "-o StrictHostKeyChecking=yes",
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
            last = (done.stderr.strip().splitlines() or [f"exit {done.returncode}"])[-1]
            raise Refused("push-failed", f"git {_verb(list(argv))} failed: {last[:300]}")
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


# ---- the owner's push to prod, and back -------------------------------------------------------------------------


def push_prod(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """Push the commit staging runs to prod; ``expected_staging_sha`` is the build the owner saw on staging."""
    expected = _sha_arg(args, "expected_staging_sha")
    with _pushing:
        sha = _staging_build(ctx)
        if sha != expected:
            raise Refused("stale", f"staging now runs {sha[:12]}, not {expected[:12]}: look again, then push")
        return _start(ctx, "push", sha)


def push_back(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """Redeploy the commit prod ran before; ``expected_sha`` is that commit as the owner saw it.

    Staging's health does not matter here: going back must work when staging is red too.
    """
    expected = _sha_arg(args, "expected_sha")
    previous = _previous_prod(ctx)
    with _pushing:
        if previous != expected:
            raise Refused("stale", f"prod's previous build is now {previous[:12]}, not {expected[:12]}: look again")
        return _start(ctx, "push back", previous)


def promote_cli(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """`factory promote [--back]` in the owner's terminal: the same push, of what staging (or prod before) runs."""
    back = args.get("back")
    if not isinstance(back, bool):
        raise Refused("promote", "say whether to push back: back must be true or false")
    if back:
        previous = _previous_prod(ctx)
        with _pushing:
            return _start(ctx, "push back", previous)
    with _pushing:
        return _start(ctx, "push", _staging_build(ctx))


def watching(ctx: Context) -> bool:
    """Whether prod is under close watch: a push to it is in flight or landed within watch.loop_minutes."""
    raw = store.read_json_or(_watch_path(ctx.places), None)
    if raw is None:
        return False
    until = raw.get("watch_until") if isinstance(raw, dict) else None
    if not isinstance(until, int | float):
        raise store.StoreError(f"{_watch_path(ctx.places)}: no watch_until")
    return ctx.clock.now() < until


def _sha_arg(args: dict[str, Any], name: str) -> str:
    value = args.get(name)
    if not isinstance(value, str) or not _SHA.match(value):
        raise Refused("push", f"{name} must be the commit sha the push sheet showed")
    return value


def _latest(ctx: Context, env: str) -> Probe | None:
    found = probes.recent(ctx.places, env, 1)
    return found[0] if found else None


def _staging_build(ctx: Context) -> str:
    staging = _latest(ctx, probes.STAGING)
    if staging is None:
        raise Refused("staging-unknown", "staging has not been probed yet: wait a minute, then push")
    if not staging.ok:
        raise Refused("staging-red", f"staging is red ({staging.error}): fix staging before pushing it to prod")
    if deploys.on_default(ctx.places, probes.STAGING) is False:
        branch = ctx.settings().get_str("repo.default_branch")
        raise Refused("staging-pinned", f"staging runs {staging.commit[:12]}, pinned off {branch}: only {branch}'s "
                                        f"checked work goes to prod; run CI on {branch} to unpin it")  # fmt: skip
    prod = _latest(ctx, probes.PROD)
    if prod is not None and prod.ok and prod.commit == staging.commit:
        raise Refused("nothing-to-push", f"prod already runs {staging.commit[:12]}, the build staging runs")
    return staging.commit


def _pushes_path(places: Places) -> Path:
    return places.state / "prod-pushes.jsonl"


def push_back_target(ctx: Context) -> str | None:
    """The build push back would put on prod, for the push sheet; None when there is none to go back to."""
    try:
        return _previous_prod(ctx)
    # telemetry: expected — before prod ran two builds there is nothing to go back to
    except Refused:
        return None


def _previous_prod(ctx: Context) -> str:
    """The build prod ran before the last push to it, as the warden saw it answer when that push began: a push that
    never answered (a crash-looping build) is never taken for one prod ran. Before the first push the warden made,
    the deploy log's previous build."""
    pushes = store.read_jsonl(_pushes_path(ctx.places))
    if pushes and pushes[-1].get("from"):
        return str(pushes[-1]["from"])
    previous = deploys.builds(ctx.places, probes.PROD)["previous"]
    if previous is None:
        raise Refused("no-previous", "the deploy log holds no earlier prod build to go back to")
    return previous["commit"]


def _start(ctx: Context, kind: str, sha: str) -> dict[str, Any]:
    """Start the operation; the caller holds _pushing."""
    current = store.read_json_or(_watch_path(ctx.places), None)
    if isinstance(current, dict) and _running(ctx, current.get("op")):
        raise Refused("push-running", f"a push to prod is already running ({current['op']}): let it finish")
    settings = ctx.settings()
    wait_s = settings.get_int("watch.push_wait_minutes") * 60
    watch_s = settings.get_int("watch.loop_minutes") * 60
    deploys.expect(ctx.places, probes.PROD, sha, ctx.clock.now(), settings.get_int("watch.push_wait_minutes"))
    running = deploys.builds(ctx.places, probes.PROD)["current"]
    store.append_jsonl(_pushes_path(ctx.places), {"at": ctx.clock.iso(), "kind": kind, "to": sha,
                                                   "from": running["commit"] if running else ""})  # fmt: skip
    with _watch_lock:  # the last step moves watch_until; it waits for this record to exist
        op_id = ops.launch(ctx, kind, _steps(ctx, kind, sha, wait_s, watch_s), {"sha": sha})
        until = ctx.clock.now() + wait_s + watch_s
        store.write_json(_watch_path(ctx.places), {"op": op_id, "kind": kind, "sha": sha, "watch_until": until})
    return ok(op=op_id, sha=sha, message=f"Pushing {sha[:12]} to prod; its steps show on the dashboard")


def _running(ctx: Context, op_id: object) -> bool:
    if not isinstance(op_id, str):
        return False
    try:
        return bool(ops.get(ctx, {"id": op_id}, None)["op"]["state"] == ops.RUNNING)
    except Refused:  # its record was pruned, so it ended long ago
        return False


def _watch_until(ctx: Context, until: float) -> None:
    with _watch_lock:
        raw = store.read_json(_watch_path(ctx.places))
        raw["watch_until"] = until
        store.write_json(_watch_path(ctx.places), raw)


def _steps(ctx: Context, kind: str, sha: str, wait_s: int, watch_s: int) -> list[tuple[str, Callable[[], str]]]:
    tick = ctx.settings().get_int("watch.tick_seconds")
    deadline = time.monotonic() + wait_s
    run: dict[str, str] = {}

    def dispatch() -> str:
        try:
            run["id"] = ctx.github.dispatch_promote(sha if _FULL_SHA.match(sha) else ctx.github.full_sha(sha))
        except GitHubError as exc:
            raise ops.StepError(str(exc)) from exc
        return f"workflow run {run['id']}"

    def finished(what: str, done: Callable[[dict[str, Any]], dict[str, Any] | None]) -> dict[str, Any]:
        """Ask GitHub about the run until ``done`` finds what finished in it; GitHub's own trouble is retried."""
        trouble = ""
        while True:
            try:
                state = ctx.github.run(run["id"])
            except GitHubError as exc:  # the run goes on at GitHub; keep asking until the deadline
                trouble = f"; GitHub last said: {exc}"
            else:
                found = done(state)
                if found is not None:
                    return found
            if time.monotonic() > deadline:
                raise ops.StepError(f"the push workflow's {what} did not finish in {wait_s // 60} minutes{trouble}")
            time.sleep(tick)

    def redeploy() -> str:
        # Prod's deploy is the workflow's `deploy` job; the releases after it must not decide whether prod moved.
        def deploy_job(state: dict[str, Any]) -> dict[str, Any] | None:
            job = state["jobs"].get(PROMOTE_JOB)
            if job is not None and job["status"] == "completed":
                return {**job, "url": state["url"]}
            if state["status"] == "completed":
                return {"conclusion": f"without a {PROMOTE_JOB} job", "url": state["url"]}
            return None

        job = finished("deploy", deploy_job)
        if job["conclusion"] == "success":
            return f"the deploy finished: {job['url']}"
        raise ops.StepError(f"the deploy ended {job['conclusion']}: {job['url']}")

    def releases() -> str:
        state = finished("releases", lambda state: state if state["status"] == "completed" else None)
        if state["conclusion"] == "success":
            return f"the releases finished: {state['url']}"
        raise ops.StepError(f"prod is live at {sha[:12]}, but the releases ended {state['conclusion']}: {state['url']}")

    def answer() -> str:
        since = ctx.clock.iso()
        while True:
            last = _latest(ctx, probes.PROD)
            if last is not None and last.at >= since and last.ok and last.commit == sha:
                return f"prod answers on {sha[:12]}"
            if time.monotonic() > deadline:
                said = "nothing yet" if last is None else (last.commit[:12] if last.ok else last.error)
                raise ops.StepError(f"prod did not answer on {sha[:12]} in {wait_s // 60} minutes; it said {said}")
            time.sleep(tick)

    def qa_prod() -> str:
        _watch_until(ctx, ctx.clock.now() + watch_s)
        # The tester looks for issues first seen since prod began to answer on this build.
        ctx.post_event(inbox.QA_PROD_DUE, {"sha": sha, "kind": kind, "since": ctx.clock.iso()})
        return "the orchestrator starts it"

    return [
        ("Starting the push workflow", dispatch),
        (f"Redeploying prod at {sha[:12]}", redeploy),
        ("Waiting for prod to answer on the new build", answer),
        ("Starting a qa-prod pass", qa_prod),
        ("Releasing what changed", releases),
    ]
