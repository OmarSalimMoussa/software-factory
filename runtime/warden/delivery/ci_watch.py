"""The product's CI after the warden's push: the run that checks, builds and deploys the pushed commit to staging.

The tick follows that run every watch.ci_poll_minutes until it completes. A run that fails files one request naming
its failed jobs, since staging then never moves to the commit; the outcome stays for `factory status` and Home.
GitHub's own trouble is noted and tried again at the next poll, as the imports do.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from runtime.queue import intake
from runtime.queue.book import Queue
from runtime.shared import store
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused
from runtime.warden import imports
from runtime.warden.context import Context
from runtime.warden.delivery import deploys
from runtime.warden.delivery.github import GitHubError
from runtime.warden.health import probes

TIMED = imports.Timed("CI watch", "following CI's run of the last push", "watch.ci_poll_minutes",
                      expected=(GitHubError, Refused, OSError), told=(GitHubError,))  # fmt: skip
FAILED = frozenset({"failure", "timed_out", "cancelled", "startup_failure"})
_KEYS = ("commit", "run", "status", "conclusion", "url", "failed", "request", *imports.TIMER_KEYS)


def _path(places: Places) -> Path:
    return places.state / "ci-run.json"


def read(places: Places) -> dict[str, Any] | None:
    """CI's run of the warden's last push: its commit, run, status, conclusion, link, failed jobs and the request it
    filed; None before the first push."""
    raw = store.read_json_or(_path(places), None)
    if raw is not None and (not isinstance(raw, dict) or set(raw) != set(_KEYS)):
        raise store.StoreError(f"{_path(places)}: want exactly {sorted(_KEYS)}")
    return raw


def poll(ctx: Context) -> None:
    pushed = deploys.last_push(ctx.places, probes.STAGING)
    if pushed is None:
        return
    state = read(ctx.places)
    if state is None or state["commit"] != pushed:
        state = {"commit": pushed, "run": None, "status": None, "conclusion": None, "url": None, "failed": [],
                 "request": None, **imports.fresh()}  # fmt: skip
    if state["status"] == "completed":
        return

    def work() -> str:
        if state["run"] is None:
            state["run"] = ctx.github.ci_run(pushed)
            if state["run"] is None:
                return f"CI has not started on {pushed[:12]} yet"
        run = ctx.github.run(state["run"])
        state.update(status=run["status"], conclusion=run["conclusion"], url=run["url"])
        if run["status"] != "completed":
            return f"CI runs {pushed[:12]}"
        if run["conclusion"] not in FAILED:
            return f"CI's run of {pushed[:12]} ended {run['conclusion']}"
        state["failed"] = sorted(n for n, job in run["jobs"].items() if job["conclusion"] in FAILED)
        state["request"] = _file(ctx, pushed, run, state["failed"])
        return f"CI's run of {pushed[:12]} failed in {', '.join(state['failed']) or 'no named job'}"

    if imports.run(ctx, TIMED, state, work):
        store.write_json(_path(ctx.places), state)


def _file(ctx: Context, commit: str, run: dict[str, Any], failed: list[str]) -> str:
    jobs = ", ".join(failed) if failed else "a job GitHub did not name"
    body = (f"CI's run for the pushed commit {commit[:12]} ended {run['conclusion']}: {jobs} failed, so staging did "
            f"not move to it. The run: {run['url']}")  # fmt: skip
    queue = Queue.of(ctx)
    with ctx.lock, queue.lock():
        return intake.file_request(ctx, queue, "agents", "warden", "normal", "CI", body).id
