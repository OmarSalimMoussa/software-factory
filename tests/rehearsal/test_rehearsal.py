"""One request walks from the owner's inbox to staging, in-process, through the warden's real dispatcher and every
real handler, each request sent by the caller who really sends it. The proof that the parts fit together."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from rehearsal_kit import Rehearsal, build, git

from runtime.queue.book import Queue
from runtime.queue.format_check import check_queue
from runtime.shared.models import FINISHED, RETIRED, WORKING
from runtime.warden import checks, crashwatch, deploys, loop, registry

TASK = "t-0001"
PICTURE = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="120" height="60"><rect x="4" y="4" width="112" height="14"/>'
    b'<rect x="4" y="22" width="112" height="10" fill="#bbb"/><text x="6" y="56">newest first</text></svg>'
)
REPORT = """# Check of master after the newest-first merge

## Scenarios

| scenario | result | steps run | evidence |
| --- | --- | --- | --- |
| add-a-note | pass | 4 | add-a-note.webm |
| find-a-note | pass | 3 | find-a-note.webm |
"""


@pytest.fixture
def rehearsal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rehearsal:
    return build(tmp_path, monkeypatch)


def kernel(tmp_path: Path) -> tuple[Path, Path]:
    """The kernel's memory, pressure and power readings for the warden's tick."""
    proc, power = tmp_path / "kernel", tmp_path / "power" / "AC"
    (proc / "pressure").mkdir(parents=True)
    (proc / "meminfo").write_text("MemAvailable:   16000000 kB\n")
    (proc / "pressure" / "cpu").write_text("some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
    power.mkdir(parents=True)
    (power / "type").write_text("Mains\n")
    (power / "online").write_text("1\n")
    return proc, power.parent


def test_one_request_goes_from_the_inbox_to_staging(rehearsal: Rehearsal, tmp_path: Path) -> None:
    r = rehearsal
    first = git(r.repo, "rev-parse", "master")

    # The owner starts the orchestrator from the dashboard; its process comes up and the backstop binds it.
    r.dashboard("orchestrator.start", {"idempotency_key": "start-orchestrator-1"})
    orch = r.run_started("orchestrator")

    # The owner files a request from their own terminal.
    said = tmp_path / "said.md"
    said.write_text("A note I just added shows at the bottom of the list; I expected it at the top.\n")
    filed = r.owner(["request", "new", "--severity", "normal", "--where", "the list of notes", "--body", str(said)])
    assert filed["inbox"] == "owner"

    # The orchestrator applies triage's plan, routes the task and proposes it with its picture.
    plan = {
        "batch": [filed["request"]],
        "new_tasks": [{
            "title": "New notes show at the bottom of the list", "body": "The page draws the oldest note first.",
            "goal_item": "G1", "severity": "major", "surfaces": ["ui"], "slices": 1, "owner_class": "none",
            "cause": "web/app.js:4 draws the notes in the order the server sent them",
            "requests": [filed["request"]], "after": [],
        }],
        "joins": {}, "noise": {}, "parked": {},
    }  # fmt: skip
    applied = r.session(orch, ["triage", "apply", "--body", r.body(r.repo, "plan.json", json.dumps(plan).encode())])
    assert applied["new"] == [TASK]
    assert r.session(orch, ["route", TASK])["rung"] == 1
    picture = r.body(r.repo, "picture.svg", PICTURE)
    r.session(orch, ["propose", TASK, "--picture", picture, "--why", "G1 is the goal's first item"])

    # The owner approves it on the dashboard.
    r.dashboard("proposal.approve", {"task": TASK, "idempotency_key": "approve-t-0001"})

    # The orchestrator starts its implementer; the session comes up in its own worktree.
    started = r.session(orch, ["start", "implementer", TASK])
    impl = r.run_started(started["name"])
    worktree = Path(registry.read(r.places)[impl].worktree)
    branch = git(worktree, "rev-parse", "--abbrev-ref", "HEAD")

    # The implementer builds its one slice, brings its slot up at the head and readies QA.
    app = worktree / "web" / "app.js"
    app.write_text(app.read_text().replace("...notes.map(", "...[...notes].reverse().map("))
    git(worktree, "commit", "-q", "-am", "Draw the newest note first")
    head = git(worktree, "rev-parse", "HEAD")
    r.session(impl, ["slot", "claim"])
    assert r.session(impl, ["slot", "up"])["exit"] == 0
    r.stack.serving = head
    testers = [t["tester"] for t in r.session(impl, ["qa", "plan"])["testers"]]
    assert testers == ["qa-ui-function", "qa-ux", "qa-visual"]
    r.session(impl, ["qa-ready", head, "--slices", head])

    # Each required tester starts (the guard's hook registers it), reports on the head and hands back.
    for n, tester in enumerate(testers):
        agent = f"a{n}7f3c9e1d2b4a6"
        r.hook(impl, "subagent.start", {"agent_id": agent, "kind": tester})
        r.session(impl, ["ready-check"], agent_id=agent)
        result = ["qa", "result", "--head", head, "--blockers", "0", "--majors", "0", "--minors", "1"]
        r.session(impl, result, agent_id=agent)
        r.hook(impl, "subagent.end", {"agent_id": agent})
    r.session(impl, ["qa-clean"])

    # The orchestrator gives the merge turn; the implementer rebases and its check passes for its tree.
    r.session(orch, ["queue", "task", "status", TASK, "turn"])
    assert registry.read(r.places)[impl].merge_turn
    git(worktree, "rebase", "-q", "master")
    assert r.session(impl, ["check"])["passed"]

    # The orchestrator merges, records the merge, saves the check of master, and ships it.
    git(r.repo, "merge", "-q", "--no-ff", branch, "-m", f"Merge {branch}: new notes show first")
    merge = git(r.repo, "rev-parse", "HEAD")
    assert checks.passed(r.places, git(r.repo, "rev-parse", "HEAD^{tree}"))
    r.session(orch, ["merge", "recorded", branch, merge])
    assert not registry.read(r.places)[impl].merge_turn
    report = r.body(r.repo, "check.md", REPORT.encode())
    assert r.session(orch, ["queue", "check", merge, "--body", report])["not_passed"] == []
    r.session(orch, ["queue", "task", "status", TASK, "checked"])
    assert r.session(orch, ["push", merge])["commits"] == 2

    # The warden's tick sees staging answer on the pushed commit, and logs it; prod still runs the first build.
    proc, power = kernel(tmp_path)
    loop.run_tick(r.warden(), r.health(staging=merge, prod=first), proc, power)
    logged = {env: deploys.builds(r.places, env)["current"] for env in ("staging", "prod")}
    assert {env: row["commit"] for env, row in logged.items() if row} == {"staging": merge, "prod": first}

    # The orchestrator's loop pass: its inbox, staging's health in its status, then its tick.
    events = [e["kind"] for e in r.session(orch, ["inbox"])["events"]]
    assert events == ["orchestrator-started", "proposal-slot-free", "approved", "loop due"]
    status = r.session(orch, ["status"])
    assert (status["staging"]["ok"], status["staging"]["commit"], status["prod"]["commit"]) == (True, merge, first)
    r.session(orch, ["tick"])

    queue = Queue.of(r.warden())
    assert queue.task(TASK).status == "on-staging"
    assert queue.task(TASK).branch == branch
    log = queue.read_log(TASK)
    assert [e["event"] for e in log] == [
        "triage", "propose", "approve", "start", "qa-ready", "qa-clean", "turn", "merged", "checked", "on-staging",
    ]  # fmt: skip
    assert [e["at"] for e in log] == sorted(e["at"] for e in log)
    assert git(r.github, "rev-parse", "master") == merge

    # Its task is past its part: the next session watch retires the implementer, though its process is alive.
    crashwatch.watch_sessions(r.warden())
    sessions = registry.read(r.places)
    assert (sessions[impl].state, sessions[impl].retired, sessions[impl].task) == (RETIRED, FINISHED, TASK)
    assert r.systemd.stops == [impl[:8]]
    tested = sorted((s.role, s.state, s.retired) for s in sessions.values() if s.parent == impl)
    assert tested == [(t, RETIRED, FINISHED) for t in testers]
    assert sessions[orch].state == WORKING

    assert [verb for verb, answer in r.answers if not answer["ok"]] == []
    assert check_queue(r.repo) == []
