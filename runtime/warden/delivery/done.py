"""Done: each task whose newest merge is on the local default branch and was not taken back, newest merge first, with
its commits and how far the merge has reached; the branch's latest commits, each with its task; and the commit
slot 0 runs, against the branch's tip.

Every ``watch.done_minutes`` the warden's mirror fetches the agents' checkout, which needs no network, under the
push lock, and a pass of its own writes the finished list for the dashboard to draw. GitHub's branch is the one the
mirror last fetched for a push: unknown before the first. Staging, which is slot 0, and prod are the builds their
health answers named. A merge's commits never change, so each merge's are read once and kept.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any

from runtime.queue.turn import merges
from runtime.shared import slices, store
from runtime.shared.paths import Places
from runtime.warden.context import Context
from runtime.warden.delivery import deploys
from runtime.warden.delivery.github_push import REVERTS, Git, fetch_checkout, pushing
from runtime.warden.health import probes

_FIELD, _END = "\x1f", "\x1e"


def path(places: Places) -> Path:
    return places.state / "done.json"


def _kept_path(places: Places) -> Path:
    return places.state / "done-merges.json"


def read(places: Places) -> dict[str, Any] | None:
    """The list the last refresh wrote; None before the first."""
    found: dict[str, Any] | None = store.read_json_or(path(places), None)
    return found


def _tried_path(places: Places) -> Path:
    return places.state / "done-tried.json"


def refresh(ctx: Context) -> None:
    """Write the list again once ``watch.done_minutes`` have passed since the last try, whether or not it worked: it
    waits for a push to end, so it runs in a thread of its own."""
    tried = store.read_json_or(_tried_path(ctx.places), None)
    if tried is not None and ctx.clock.age(tried["at"]) < ctx.settings().get_int("watch.done_minutes") * 60:
        return
    store.write_json(_tried_path(ctx.places), {"at": ctx.clock.iso()})
    branch = ctx.settings().get_str("repo.default_branch")
    with pushing:
        git = Git(ctx)
        local = fetch_checkout(ctx, git)
        github = _full(git, f"refs/github/{branch}")
    recorded = merges(ctx.places)
    lines = git.run("log", "--first-parent", "--merges", "--format=%H %ct", local).splitlines()
    landed = [(sha, int(at)) for sha, at in (line.split() for line in lines) if sha in recorded]
    kept = _kept(ctx.places, git, [sha for sha, _ in landed])
    reverted = _taken_back(git, local)
    newest: dict[str, tuple[str, int]] = {}
    for sha, at in landed:
        newest.setdefault(recorded[sha], (sha, at))
    reach = {"github": _ancestors(git, github)}
    reach.update({env: _ancestors(git, _build(ctx, git, env)) for env in probes.ENVS})
    tasks = [
        {"task": task, "merge": sha, "at": at, "commits": kept[sha],
         **{stage: None if seen is None else sha in seen for stage, seen in reach.items()}}
        for task, (sha, at) in newest.items() if sha not in reverted
    ]  # fmt: skip
    owner = {c["sha"]: recorded[sha] for sha, rows in kept.items() for c in rows}
    latest = _commits(git, "-n", str(ctx.settings().get_int("dashboard.done_commits")), local)
    done = {
        "at": ctx.clock.iso(),
        "local": local,
        "github": github,
        "ahead": None if github is None else int(git.run("rev-list", "--count", f"{github}..{local}")),
        "slot0": _slot0(ctx, git, local),
        "tasks": tasks,
        "commits": [{**c, "task": owner.get(c["sha"])} for c in latest],
    }
    store.write_json(path(ctx.places), done, mode=0o600)


def _taken_back(git: Git, local: str) -> set[str]:
    """The commits a revert took back and no later revert brought back: a revert of a revert undoes the first."""
    reverts: dict[str, list[str]] = {}
    for record in git.run("log", f"--format=%H{_FIELD}%B{_END}", "--grep=This reverts commit", local).split(_END):
        if record.strip():
            sha, body = record.lstrip("\n").split(_FIELD, 1)
            for target in REVERTS.findall(body):
                reverts.setdefault(target, []).append(sha)

    @cache
    def undone(commit: str) -> bool:
        return any(not undone(revert) for revert in reverts.get(commit, []))

    return {commit for commit in reverts if undone(commit)}


def _full(git: Git, name: str) -> str | None:
    """The full sha of a ref or commit the mirror holds; None when it holds none by that name."""
    return git.run("rev-parse", "--verify", f"{name}^{{commit}}").strip() if git.knows(name) else None


def _ancestors(git: Git, commit: str | None) -> set[str] | None:
    return None if commit is None else set(git.run("rev-list", commit).split())


def _build(ctx: Context, git: Git, env: str) -> str | None:
    """The commit ``env`` last answered on, when the mirror holds it."""
    current = deploys.builds(ctx.places, env)["current"]
    return None if current is None else _full(git, current["commit"])


def _kept(places: Places, git: Git, landed: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Each landed merge's own commits: read once, from the mirror, and kept."""
    kept: dict[str, list[dict[str, Any]]] = store.read_json_or(_kept_path(places), {})
    new = [sha for sha in landed if sha not in kept]
    for sha in new:
        kept[sha] = _commits(git, f"{sha}^1..{sha}")
    if new:
        store.write_json(_kept_path(places), kept, mode=0o600)
    return kept


def _commits(git: Git, *args: str) -> list[dict[str, Any]]:
    """Each commit git log lists, merges left out: its sha, time, first line and slice."""
    out = git.run("log", "--no-merges", f"--format=%H{_FIELD}%ct{_FIELD}%B{_END}", *args)
    rows = []
    for record in out.split(_END):
        if record.strip():
            sha, at, body = record.lstrip("\n").split(_FIELD, 2)
            rows.append({"sha": sha, "at": int(at), "subject": body.strip().split("\n", 1)[0], "slice": _slice(body)})
    return rows


def _slice(body: str) -> str | None:
    try:
        found = slices.mark(body)
    # telemetry: expected — the guard refuses a commit naming two places; one made outside the factory shows none
    except slices.SliceError:
        return None
    return None if found is None else f"{found[0]}/{found[1]}"


def _slot0(ctx: Context, git: Git, local: str) -> dict[str, Any]:
    """The commit slot 0 runs, as staging's last probe named it, and how many commits it is behind the branch's tip;
    or why it gave none."""
    found = probes.recent(ctx.places, probes.STAGING, 1)
    if not found or not found[0].ok:
        why = found[0].error if found else "staging has not been probed yet"
        return {"commit": None, "behind": None, "why": why}
    commit = found[0].commit
    full = _full(git, commit)
    on_branch = full is not None and git.is_ancestor(full, local)
    behind = int(git.run("rev-list", "--count", f"{full}..{local}")) if on_branch else None
    return {"commit": commit, "behind": behind, "why": None}
