"""The owner's push to prod, and back: an operation the dashboard follows step by step.

It starts the product's prod workflow (the factory's template, copied in) for exactly the commit staging runs, or the
one prod ran before as the deploy log has it, waits for prod to answer on it, then asks the orchestrator for a qa-prod
pass. The owner's code is the only lock on it. While a push to prod is in flight, and for watch.loop_minutes after it
lands, the loop probes prod every tick.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from runtime.shared import operations, store
from runtime.shared.models import Probe
from runtime.shared.paths import PROMOTE_JOB, Places
from runtime.shared.protocol import Refused, ok
from runtime.shared.settings import Settings
from runtime.warden import inbox
from runtime.warden.context import Context
from runtime.warden.delivery import deploys
from runtime.warden.delivery.github import GitHubError
from runtime.warden.delivery.github_push import FULL_SHA, Git, pushing
from runtime.warden.health import probes
from runtime.warden.owner import ops

log = logging.getLogger(__name__)
_SHA = re.compile(r"^[0-9a-f]{7,40}$")
_watch_lock = threading.Lock()


def _watch_path(places: Places) -> Path:
    return places.state / "push.json"


def push_prod(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """Push the commit staging runs to prod; ``expected_staging_sha`` is the build the owner saw on staging."""
    expected = _sha_arg(args, "expected_staging_sha")
    with pushing:
        sha = _staging_build(ctx)
        if sha != expected:
            raise Refused("stale", f"staging now runs {sha[:12]}, not {expected[:12]}: look again, then push")
        return _start(ctx, operations.PUSH, sha)


def push_back(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """Redeploy the commit prod ran before; ``expected_sha`` is that commit as the owner saw it.

    Staging's health does not matter here: going back must work when staging is red too.
    """
    expected = _sha_arg(args, "expected_sha")
    previous = _previous_prod(ctx)
    with pushing:
        if previous != expected:
            raise Refused("stale", f"prod's previous build is now {previous[:12]}, not {expected[:12]}: look again")
        return _start(ctx, operations.PUSH_BACK, previous)


def promote_cli(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """`factory promote [--back]` in the owner's terminal: the same push, of what staging (or prod before) runs."""
    back = args.get("back")
    if not isinstance(back, bool):
        raise Refused("promote", "say whether to push back: back must be true or false")
    if back:
        previous = _previous_prod(ctx)
        with pushing:
            return _start(ctx, operations.PUSH_BACK, previous)
    with pushing:
        return _start(ctx, operations.PUSH, _staging_build(ctx))


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
    # Only a build known to be on GitHub's default branch goes.
    on_default = deploys.on_default(ctx.places, probes.STAGING)
    if on_default is not True:
        raise Refused("staging-pinned", staging_off_default(ctx.settings(), on_default))
    prod = _latest(ctx, probes.PROD)
    if prod is not None and prod.ok and prod.commit == staging.commit:
        raise Refused("nothing-to-push", f"prod already runs {staging.commit[:12]}, the build staging runs")
    return staging.commit


def staging_off_default(settings: Settings, on_default: bool | None) -> str:
    """Why staging's build may not go to prod, when it is not known to be on the default branch."""
    branch = settings.get_str("repo.default_branch")
    if on_default is False:
        return (f"Staging runs a build that is not on {branch} as GitHub has it: only {branch}'s checked work goes "
                f"to prod. Run CI on {branch} to put staging back on it.")  # fmt: skip
    return (
        f"The factory cannot tell yet whether staging's build is on {branch}: GitHub did not answer. It asks "
        f"again every {settings.get_int('watch.branch_retry_minutes')} minutes; the push waits until then."
    )


def ask_branch(ctx: Context) -> None:
    """The warden's branch pass, in a thread of its own, since a fetch can take minutes: whether staging's build is
    on the default branch, which a push to prod needs. Asked once for a build, and again every
    watch.branch_retry_minutes while GitHub has not said, so one failed fetch never holds a build back."""
    current = deploys.builds(ctx.places, probes.STAGING)["current"]
    if current is None:
        return
    commit = str(current["commit"])
    asked = deploys.branch_asked(ctx.places, probes.STAGING, commit)
    retry = ctx.settings().get_int("watch.branch_retry_minutes") * 60
    if asked is not None and (asked[0] is not None or ctx.clock.now() - asked[1] < retry):
        return
    answer: bool | None
    try:
        answer = on_default(ctx, commit)
    except Refused as exc:
        answer = None
        if asked is None:
            log.warning("warden: whether staging's build %s is on the default branch is unknown (%s)", commit[:12],
                        exc.reason)  # fmt: skip
    deploys.note_branch(ctx.places, probes.STAGING, commit, answer, ctx.clock.now())


def on_default(ctx: Context, commit: str) -> bool:
    """Whether ``commit`` is on GitHub's default branch, from a fresh fetch into the warden's mirror: False for one
    the branch does not hold. Raises Refused when GitHub or the mirror did not answer."""
    settings = ctx.settings()
    branch = settings.get_str("repo.default_branch")
    git = Git(ctx)
    git.run("fetch", "--no-tags", "--quiet", f"git@github.com:{settings.get_str('repo.github')}.git",
            f"+refs/heads/{branch}:refs/github/{branch}")  # fmt: skip
    if git._exec(["cat-file", "-e", f"{commit}^{{commit}}"], cwd=git.mirror).returncode != 0:
        return False
    return git.is_ancestor(commit, f"refs/github/{branch}")


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
    """Start the operation; the caller holds pushing."""
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
    # telemetry: expected — its record was pruned, so the operation ended long ago
    except Refused:
        return False


def released_path(places: Places) -> Path:
    return places.state / "released.json"


def released(places: Places) -> str | None:
    """The commit the last releases that finished ran at; None before any did."""
    found = store.read_json_or(released_path(places), None)
    if found is None:
        return None
    if not isinstance(found, dict) or not isinstance(found.get("commit"), str):
        raise store.StoreError(f"{released_path(places)}: want a commit")
    return str(found["commit"])


def qa_prod_path(places: Places) -> Path:
    return places.state / "qa-prod-due.json"


def post_qa_prod(ctx: Context, sha: str, kind: str, since: str) -> None:
    """Ask the orchestrator for a read-only walk of prod at ``sha``, looking at issues first seen since ``since``; the
    loop asks again qa.prod_every_hours after the last ask. The tester's brief takes prod's and staging's addresses
    from it."""
    try:
        where = probes.origins(ctx)
    # telemetry: surfaced — the event says why it has no addresses, in `where`, and the brief carries it on
    except probes.NoAddress as exc:
        where = {"addresses": f"unknown: {exc}"}
    ctx.post_event(inbox.QA_PROD_DUE, {"sha": sha, "kind": kind, "since": since, **where})
    store.write_json(qa_prod_path(ctx.places), {"at": ctx.clock.now(), "iso": ctx.clock.iso()})


def _watch_until(ctx: Context, until: float) -> None:
    with _watch_lock:
        raw = store.read_json(_watch_path(ctx.places))
        raw["watch_until"] = until
        store.write_json(_watch_path(ctx.places), raw)


def _steps(ctx: Context, kind: str, sha: str, wait_s: int, watch_s: int) -> list[tuple[str, Callable[[], str]]]:
    push = _Push(ctx, kind, sha, wait_s, watch_s)
    return [
        ("Starting the push workflow", push.dispatch),
        (f"Redeploying prod at {sha[:12]}", push.redeploy),
        ("Waiting for prod to answer on the new build", push.answer),
        ("Starting a qa-prod pass", push.qa_prod),
        ("Releasing what changed", push.releases),
    ]


def _deploy_job(state: dict[str, Any]) -> dict[str, Any] | None:
    """Prod's deploy is the workflow's `deploy` job; the releases after it must not decide whether prod moved."""
    job = state["jobs"].get(PROMOTE_JOB)
    if job is not None and job["status"] == "completed":
        return {**job, "url": state["url"]}
    if state["status"] == "completed":
        return {"conclusion": f"without a {PROMOTE_JOB} job", "url": state["url"]}
    return None


class _Push:
    """One push's steps, sharing its workflow run and one deadline for every wait."""

    def __init__(self, ctx: Context, kind: str, sha: str, wait_s: int, watch_s: int) -> None:
        self.ctx, self.kind, self.sha, self.wait_s, self.watch_s = ctx, kind, sha, wait_s, watch_s
        self.tick = ctx.settings().get_int("watch.tick_seconds")
        self.deadline = time.monotonic() + wait_s
        self.run_id = ""
        self.full_sha = sha

    def dispatch(self) -> str:
        ctx = self.ctx
        try:
            self.full_sha = self.sha if FULL_SHA.match(self.sha) else ctx.github.full_sha(self.sha)
            self.run_id = ctx.github.dispatch_promote(self.full_sha, released(ctx.places) or "-")
        except GitHubError as exc:
            raise ops.StepError(str(exc)) from exc
        return f"workflow run {self.run_id}"

    def _finished(self, what: str, done: Callable[[dict[str, Any]], dict[str, Any] | None]) -> dict[str, Any]:
        """Ask GitHub about the run until ``done`` finds what finished in it; GitHub's own trouble is retried."""
        trouble = ""
        while True:
            try:
                state = self.ctx.github.run(self.run_id)
            # telemetry: surfaced — the run goes on at GitHub; `trouble` names GitHub's last word if the wait ends
            except GitHubError as exc:
                trouble = f"; GitHub last said: {exc}"
            else:
                found = done(state)
                if found is not None:
                    return found
            if time.monotonic() > self.deadline:
                raise ops.StepError(
                    f"the push workflow's {what} did not finish in {self.wait_s // 60} minutes{trouble}"
                )
            time.sleep(self.tick)

    def redeploy(self) -> str:
        job = self._finished("deploy", _deploy_job)
        if job["conclusion"] == "success":
            return f"the deploy finished: {job['url']}"
        raise ops.StepError(f"the deploy ended {job['conclusion']}: {job['url']}")

    def releases(self) -> str:
        state = self._finished("releases", lambda state: state if state["status"] == "completed" else None)
        if state["conclusion"] == "success":
            # The next push's releases compare with this commit; one that failed is tried again then.
            store.write_json(released_path(self.ctx.places), {"commit": self.full_sha, "at": self.ctx.clock.iso()})
            return f"the releases finished: {state['url']}"
        raise ops.StepError(
            f"prod is live at {self.sha[:12]}, but the releases ended {state['conclusion']}: {state['url']}"
        )

    def answer(self) -> str:
        since, sha = self.ctx.clock.iso(), self.sha
        while True:
            last = _latest(self.ctx, probes.PROD)
            if last is not None and last.at >= since and last.ok and last.commit == sha:
                return f"prod answers on {sha[:12]}"
            if time.monotonic() > self.deadline:
                said = "nothing yet" if last is None else (last.commit[:12] if last.ok else last.error)
                raise ops.StepError(f"prod did not answer on {sha[:12]} in {self.wait_s // 60} minutes; it said {said}")
            time.sleep(self.tick)

    def qa_prod(self) -> str:
        _watch_until(self.ctx, self.ctx.clock.now() + self.watch_s)
        # The tester looks for issues first seen since prod began to answer on this build.
        post_qa_prod(self.ctx, self.sha, self.kind, self.ctx.clock.iso())
        return "the orchestrator starts it"
