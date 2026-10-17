"""The owner's push to prod, and back: an operation the dashboard follows step by step.

A push to prod takes slot 0's build, the commit staging's last probe named, once it is cleared: staging answers on it,
it is on the checkout's default branch, its tree passed factory check, every merge since prod's build passed its
check, and prod does not run it yet. Push back takes the build prod ran before, and needs only that it is on the
branch, since going back must work while staging is red. The deploy unit builds the commit on this machine and
deploys it; the push then waits for prod to answer on it and asks the orchestrator for a qa-prod pass. The owner's code
is the only lock on it. While a push to prod is in flight, and for watch.loop_minutes after it lands, the loop probes
prod every tick.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from runtime.queue.book import Queue
from runtime.queue.turn import merges
from runtime.shared import operations, store
from runtime.shared.models import ON_STAGING, Probe
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused, ok
from runtime.warden import checks, inbox
from runtime.warden.context import Context
from runtime.warden.delivery import deploys
from runtime.warden.delivery.github_push import REVERTS, Git, fetch_checkout, pushing
from runtime.warden.health import probes
from runtime.warden.owner import ops
from runtime.warden.sessions.launch import LaunchError

_SHA = re.compile(r"^[0-9a-f]{7,40}$")
_watch_lock = threading.Lock()
# A task whose merge passed its check of the default branch. A closed task's merge ships only on a saved check of
# that merge that passed: a task may be closed before its check.
SHIPPABLE = frozenset({"checked", ON_STAGING, "promoted"})


def _watch_path(places: Places) -> Path:
    return places.state / "push.json"


def push_prod(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """Push slot 0's build to prod; ``expected_staging_sha`` is the build the owner saw on staging."""
    expected = _sha_arg(args, "expected_staging_sha")
    with pushing:
        sha = _slot0_build(ctx)
        if sha != expected:
            raise Refused("stale", f"staging now runs {sha[:12]}, not {expected[:12]}: look again, then push")
        return _start(ctx, operations.PUSH, sha)


def push_back(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """Redeploy the commit prod ran before; ``expected_sha`` is that commit as the owner saw it."""
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
        return _start(ctx, operations.PUSH, _slot0_build(ctx))


def push_refused(ctx: Context) -> str | None:
    """Why a push to prod would be refused, as far as the probes tell: staging red or unknown, or prod running its
    build; None otherwise. The push asks git about the commit when it starts."""
    try:
        _not_on_prod(ctx, _slot0_build(ctx))
    # telemetry: surfaced — the push sheet draws `exc.reason` in place of its button
    except Refused as exc:
        return exc.reason
    return None


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


def _slot0_build(ctx: Context) -> str:
    """The build slot 0 runs, as staging's last probe named it; refused while staging is red or unknown."""
    staging = _latest(ctx, probes.STAGING)
    if staging is None:
        raise Refused("staging-unknown", "staging has not been probed yet: wait a minute, then push")
    if not staging.ok:
        raise Refused("staging-red", f"staging is red ({staging.error}): fix staging before pushing it to prod")
    return staging.commit


def _not_on_prod(ctx: Context, sha: str) -> None:
    prod = _latest(ctx, probes.PROD)
    if prod is not None and prod.ok and prod.commit == sha:
        raise Refused("nothing-to-push", f"prod already runs {sha[:12]}")


def _cleared(ctx: Context, kind: str, sha: str) -> str:
    """The full commit of ``sha``, from a fresh fetch of the agents' checkout, once it may go: on the default branch,
    and for a push to prod, with a tree that passed factory check and only checked merges since prod's build. The
    caller holds pushing."""
    branch = ctx.settings().get_str("repo.default_branch")
    git = Git(ctx)
    tip = fetch_checkout(ctx, git)
    if not git.knows(sha) or not git.is_ancestor(sha, tip):
        raise Refused("off-branch", f"{sha[:12]} is not on {branch} in the checkout: only {branch}'s work goes to prod")
    full = git.run("rev-parse", "--verify", f"{sha}^{{commit}}").strip()
    if kind == operations.PUSH_BACK:
        return full
    if not checks.passed(ctx.places, git.run("rev-parse", f"{full}^{{tree}}").strip()):
        raise Refused("unchecked", f"{sha[:12]} has not passed factory check: run factory check on {branch}, then push")
    prod = deploys.builds(ctx.places, probes.PROD)["current"]
    unchecked = [] if prod is None else unchecked_merges(ctx, git, prod["commit"], full)
    if unchecked:
        raise Refused("uncleared", f"{sha[:12]} would ship {', '.join(unchecked)}, whose check of the default "
                                   "branch has not passed: check it first, or revert it")  # fmt: skip
    return full


def unchecked_merges(ctx: Context, git: Git, base: str, head: str) -> list[str]:
    """The merges on the default branch between ``base`` and ``head`` that are not cleared to ship: one the warden
    never saw land for a task, or whose task's check has not passed. A merge reverted within the range ships nothing
    of its own."""
    reverted = set(REVERTS.findall(git.run("log", "--format=%B", f"{base}..{head}")))
    found = merges(ctx.places)
    queue = Queue.of(ctx)
    passed = {c.commit for c in queue.checks()[0] if all(r.result == "pass" for r in c.rows)}
    out = []
    for merge in git.run("rev-list", "--first-parent", "--merges", f"{base}..{head}").split():
        if merge in reverted:
            continue
        task = found.get(merge)
        status = queue.task(task).status if task else ""
        if status not in SHIPPABLE and not (status == "closed" and merge in passed):
            out.append(f"merge {merge[:12]}" + (f" ({task}, {status})" if task else " (no task's merge)"))
    return out


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
    """Start the operation once ``sha`` is cleared to go; the caller holds pushing."""
    current = running(ctx)
    if current is not None:
        raise Refused("push-running", f"a push to prod is already running ({current}): let it finish")
    _not_on_prod(ctx, sha)
    full = _cleared(ctx, kind, sha)
    settings = ctx.settings()
    wait_s = settings.get_int("watch.push_wait_minutes") * 60
    watch_s = settings.get_int("watch.loop_minutes") * 60
    deploys.expect(ctx.places, probes.PROD, sha, ctx.clock.now(), settings.get_int("watch.push_wait_minutes"))
    live = deploys.builds(ctx.places, probes.PROD)["current"]
    store.append_jsonl(_pushes_path(ctx.places), {"at": ctx.clock.iso(), "kind": kind, "to": sha,
                                                   "from": live["commit"] if live else ""})  # fmt: skip
    with _watch_lock:  # the last step moves watch_until; it waits for this record to exist
        op_id = ops.launch(ctx, kind, _steps(_Push(ctx, kind, sha, full, watch_s)), {"sha": sha})
        until = ctx.clock.now() + wait_s + watch_s
        store.write_json(_watch_path(ctx.places), {"op": op_id, "kind": kind, "sha": sha, "watch_until": until})
    return ok(op=op_id, sha=sha, message=f"Pushing {sha[:12]} to prod; its steps show on the dashboard")


def running(ctx: Context) -> str | None:
    """The push to prod or back that is still running, by its operation's id; None when none is."""
    current = store.read_json_or(_watch_path(ctx.places), None)
    op_id = current.get("op") if isinstance(current, dict) else None
    if not isinstance(op_id, str):
        return None
    try:
        return op_id if ops.get(ctx, {"id": op_id}, None)["op"]["state"] == ops.RUNNING else None
    # telemetry: expected — its record was pruned, so the operation ended long ago
    except Refused:
        return None


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


def _failure(output: str, code: int) -> str:
    """What the deploy's log says went wrong: its last `failed:` line, and the problems listed under it."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    marks = [n for n, line in enumerate(lines) if line.startswith("failed:")]
    if not marks:
        return f"the build and deploy exited {code}, and its log names no failure"
    return " ".join(lines[marks[-1] :]).removeprefix("failed:").strip()[:300]


def _steps(push: _Push) -> list[tuple[str, Callable[[], str]]]:
    return [
        (f"Building and deploying prod at {push.sha[:12]}", push.deploy),
        ("Waiting for prod to answer on the new build", push.answer),
        ("Starting a qa-prod pass", push.qa_prod),
    ]


class _Push:
    """One push's steps."""

    def __init__(self, ctx: Context, kind: str, sha: str, full: str, watch_s: int) -> None:
        self.ctx, self.kind, self.sha, self.full, self.watch_s = ctx, kind, sha, full, watch_s
        self.tick = ctx.settings().get_int("watch.tick_seconds")

    def deploy(self) -> str:
        """The deploy unit builds the images the commit lacks on this machine, then deploys them; it is handed the
        full commit, since the probes name a build by its short one."""
        try:
            code, output = self.ctx.launcher.deploy_prod(self.full, self.kind == operations.PUSH_BACK)
        except LaunchError as exc:
            raise ops.StepError(str(exc)) from exc
        if code != 0:
            raise ops.StepError(_failure(output, code))
        return f"prod is deployed at {self.full[:12]}"

    def answer(self) -> str:
        """The warden's own probe sees prod on the build, as the deploy did, within the time its probes take to count
        an outage."""
        settings = self.ctx.settings()
        wait = settings.get_int("watch.probe_seconds") * settings.get_int("watch.outage_after") + self.tick
        since, sha, deadline = self.ctx.clock.iso(), self.sha, time.monotonic() + wait
        while True:
            last = _latest(self.ctx, probes.PROD)
            if last is not None and last.at >= since and last.ok and last.commit == sha:
                return f"prod answers on {sha[:12]}"
            if time.monotonic() > deadline:
                said = "nothing yet" if last is None else (last.commit[:12] if last.ok else last.error)
                raise ops.StepError(f"prod did not answer on {sha[:12]} in {wait} seconds; it said {said}")
            time.sleep(self.tick)

    def qa_prod(self) -> str:
        _watch_until(self.ctx, self.ctx.clock.now() + self.watch_s)
        # The tester looks for issues first seen since prod began to answer on this build.
        post_qa_prod(self.ctx, self.sha, self.kind, self.ctx.clock.iso())
        return "the orchestrator starts it"
