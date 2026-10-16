"""The product's CI after the warden's push: the run that checks, builds and deploys the pushed commit to staging.

A pass of its own, off the tick, follows that run every watch.ci_poll_minutes until it completes. A run that fails
files one request naming its failed jobs, since staging then never moves to the commit; Home and `factory status`
show the outcome. Agents cannot open GitHub, so the request says which steps failed and quotes each failed job's log
up to its last error mark, where the job failed before its clean-up steps: the end of that, within watch.ci_log_kib.
It is filed before the run counts as completed: one that cannot be filed, like GitHub's own trouble, is tried again
at the next poll.
"""

from __future__ import annotations

import logging
import re
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
_STAMP = re.compile(r"^\ufeff?\d{4}-\d\d-\d\dT[\d:.]+Z ")  # the time GitHub puts before each line of a log
_UNSEEN = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|[\x00-\x08\x0b-\x1f\x7f]")
_ERROR = "##[error]"

log = logging.getLogger(__name__)


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
    if state["status"] != "completed" and imports.run(ctx, TIMED, state, lambda: _follow(ctx, pushed, state)):
        store.write_json(_path(ctx.places), state)


def _follow(ctx: Context, pushed: str, state: dict[str, Any]) -> str:
    if state["run"] is None:
        state["run"] = ctx.github.ci_run(pushed)
        if state["run"] is None:
            return f"CI has not started on {pushed[:12]} yet"
    run = ctx.github.run(state["run"])
    if run["status"] == "completed" and run["conclusion"] in FAILED:
        failed = sorted(n for n, job in run["jobs"].items() if job["conclusion"] in FAILED)
        state.update(failed=failed, request=_file(ctx, pushed, run, failed))
    state.update(status=run["status"], conclusion=run["conclusion"], url=run["url"])
    if run["status"] != "completed":
        return f"CI runs {pushed[:12]}"
    if run["conclusion"] not in FAILED:
        return f"CI's run of {pushed[:12]} ended {run['conclusion']}"
    return f"CI's run of {pushed[:12]} failed in {', '.join(state['failed']) or 'no named job'}"


def _file(ctx: Context, commit: str, run: dict[str, Any], failed: list[str]) -> str:
    jobs = ", ".join(failed) if failed else "a job GitHub did not name"
    said = (f"CI's run for the pushed commit {commit[:12]} ended {run['conclusion']}: {jobs} failed, so staging did "
            f"not move to it. The run: {run['url']}")  # fmt: skip
    body = "\n\n".join([said, *(_why(ctx, name, run["jobs"][name]) for name in failed)])
    queue = Queue.of(ctx)
    with ctx.lock, queue.lock():
        return intake.file_request(ctx, queue, "agents", "warden", "normal", "CI", body).id


def _why(ctx: Context, name: str, job: dict[str, Any]) -> str:
    if job["steps"] is None:
        said = f"{name} failed, and GitHub listed none of its steps"
    else:
        failed = [f'"{s["name"]}"' if s["name"] else "a step GitHub did not name" for s in job["steps"]
                  if s["conclusion"] in FAILED]  # fmt: skip
        said = f"{name} failed at {', '.join(failed)}" if failed else f"{name} failed outside its steps"
    settings = ctx.settings()
    try:
        if job["id"] is None:
            raise GitHubError("GitHub named no id for the job")
        text = ctx.github.job_log(job["id"], settings.get_int("watch.ci_log_mib") * 2**20)
    except GitHubError as exc:
        log.warning("warden: the log of CI job %s (id %s) could not be read: %s", name, job["id"], exc)
        return f"{said}. Its log could not be read: {exc}"
    lines = [_UNSEEN.sub("", _STAMP.sub("", line)) for line in text.splitlines()]
    end = next((n + 1 for n in range(len(lines) - 1, -1, -1) if lines[n].startswith(_ERROR)), None)
    whole = "\n".join(lines[:end]).encode()
    budget = settings.get_int("watch.ci_log_kib") * 1024
    shown = whole[-budget:].decode(errors="ignore")
    if len(whole) > budget and "\n" in shown:
        shown = shown.partition("\n")[2]
    where = "up to its last error" if end else "to its end"
    return f"{said}. Its log, {where}:\n\n" + "\n".join(f"    {line}" for line in shown.splitlines())
