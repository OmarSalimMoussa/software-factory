"""A warden state folder and a product checkout that hold what the UI's fixtures show, in the real formats.

The fixtures (testdata/dashboard-fixtures) are the UI's picture of one busy afternoon; this module writes the same
afternoon as the warden keeps it: registry, queue files, cards, proposals, costs, probes, deploys, plan limits,
vitals and reflections. The dashboard's views are then built from it, and must give back payloads of the fixtures'
shapes.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import time
from pathlib import Path
from typing import Any

from seed_agents import history_lines, seed_check_runs, seed_cost, seed_evidence, seed_sessions, seed_transcript
from seed_kit import FIXTURES, PRICES, ROOT, fixture, fixture_time, iso, write
from seed_queue import seed_checks, seed_requests, seed_tasks
from settings_text import set_line

from runtime.queue import formats, owner, records, turn
from runtime.shared import claude_accounts, models, store, totp
from runtime.shared import settings as settings_module
from runtime.shared.paths import Places
from runtime.warden import public
from runtime.warden.delivery import deploys, done
from runtime.warden.sessions import registry

T0 = 1_791_546_000.0  # 2026-10-09 11:40:00 UTC
TEST_TOKEN = "1"  # the device token the UI drives send as their owner cookie
STAGING, PROD, PREVIOUS = "a1b2c3d", "9f8e7d1", "5c4d3e2"  # a health answer may name a short commit
GOAL = """# The goal

## Who it is for
Small teams that send invoices every week.

## The goal
- G1 New teams sign up and send their first invoice
- G2 A first import runs end to end
- G3 Every screen fits a phone
- G4 Feedback reaches the queue the same day
- G5 Install from the app store
"""


def defaults_text(repo: Path) -> str:
    text = (ROOT / "settings.toml").read_text()
    for key, value in {
        "repo.path": str(repo),
        "repo.github": "acme/acme-app",
    }.items():
        text = set_line(text, key, value)
    prices = "".join(
        f'"{m}" = {{ input = {i}, output = {o}, cache_read = 0, cache_write = 0 }}\n' for m, (i, o) in PRICES.items()
    )
    # The seed's own prices stand in for the shipped ones: the table runs from its header to the next blank line.
    head, header, rest = text.partition("[prices]")
    comment, _, after = rest.partition("\n")
    return head + header + comment + "\n" + prices + after[after.index("\n\n") :]


def _seed_queue(repo: Path, now: float) -> list[models.Task]:
    """The product's queue: its tasks with their logs, requests, specs and checks, and two files someone broke."""
    (repo / "GOAL.md").write_text(GOAL)
    tasks, logs = seed_tasks(repo, now)
    requests = seed_requests(tasks, now)
    for task in tasks:
        write(repo / "queue" / "tasks" / f"{task.id}.md", formats.dump_task(task))
        write(
            repo / "queue" / "tasks" / f"{task.id}.log.jsonl",
            "".join(records.dump_log(e).decode() for e in logs[task.id]),
        )
    for req in requests:
        write(repo / "queue" / "requests" / req.inbox / f"{req.id}.md", formats.dump_request(req))
    for task_id, status, title in (
        ("t-0012", "approved", "Usage page: totals per day"),
        ("t-0014", "draft", "Clearer empty states"),
        ("t-0023", "review", "Sign-in from the build step"),
    ):
        spec = records.Spec(
            task_id,
            "G2",
            3,
            status,
            "v1",
            iso(now - 86400),
            title,
            "Usage shows each day's spend as a bar.",
        )
        write(repo / "queue" / "specs" / f"{task_id}.md", records.dump_spec(spec))
    seed_checks(repo, now)
    write(repo / "queue" / "tasks" / "t-0026.md", "---\nid: t-0026\n---\n# A task file someone broke\n")
    write(repo / "queue" / "requests" / "users" / "r-2026-10-07-0032.md", "not a request\n")
    return tasks


def _seed_cards(places: Places, tasks: list[models.Task], now: float) -> None:
    """The owner's three cards, two of them with their pictures."""
    cards = []
    for n, (kind, task_id, ask) in enumerate(
        (
            ("spec", "t-0023", "Sign off the partner sign-in spec"),
            ("decide", "t-0019", "Its rounds are used up at rung 3."),
            ("question", "t-0022", "Resume from the last part, or start again?"),
        ),
        1,
    ):
        picture = ""
        if kind != "question":
            data = (FIXTURES / "pictures" / f"card-n{n}.svg").read_bytes()
            picture = f"n{n}-{hashlib.sha256(data).hexdigest()[:12]}.svg"
            places.pictures.mkdir(parents=True, exist_ok=True)
            (places.pictures / picture).write_bytes(data)
        cards.append(
            models.Card(
                id=f"n{n}",
                kind=kind,
                task=task_id,
                title=next(t.title for t in tasks if t.id == task_id),
                ask=ask,
                created=iso(now - 3600),
                picture=picture,
                brief="v1" if kind == "spec" else "",
                asked_by="impl-retry-upload",
            )
        )
    store.write_json(places.cards, [models.dump(c) for c in cards])


def _seed_briefs(places: Places, now: float) -> None:
    page = (FIXTURES / "briefs" / "brief.html").read_text()
    for task_id in ("t-0012", "t-0014", "t-0023"):
        threads = []
        if task_id == "t-0023":
            threads = [
                {"id": "c1", "version": 1, "quote": "in place, not on its own page", "prefix": "show the form ",
                 "suffix": ". I recommend", "note": "What happens on a phone?", "at": iso(now - 1800),
                 "state": "open", "replies": []},
                {"id": "c2", "version": 1, "quote": "keeps what the partner typed", "prefix": "The form ",
                 "suffix": " in the step", "note": "Even a pasted file?", "at": iso(now - 5400), "state": "answered",
                 "replies": [{"by": "designer", "text": "Yes: the file waits in the step.", "at": iso(now - 3600)}]},
            ]  # fmt: skip
        store.write_text(places.state / "briefs" / task_id / "v1.html", page, mode=0o600)
        store.write_json(places.state / "briefs" / task_id / "brief.json",
                         {"versions": [{"n": 1, "at": iso(now - 7200)}], "threads": threads}, mode=0o600)  # fmt: skip


def _seed_health(places: Places, now: float) -> None:
    """Prod's and staging's probes and deploys, the plan's limits, the vitals, the disk and the last triage."""
    for minute in range(200, 0, -1):
        for env, commit in (("staging", STAGING), ("prod", PROD)):
            ok = not (env == "staging" and minute == 193)
            probe = models.Probe(
                iso(now - minute * 60), env, ok, commit if ok else "", 80, "" if ok else "HTTP 502", counts=not ok
            )
            store.append_jsonl(places.probes, models.dump(probe))
    for when, env, commit in (("Oct 5, 09:12", "prod", PREVIOUS), ("Oct 7, 10:05", "prod", PROD),
                              ("Oct 9, 11:18", "staging", STAGING)):  # fmt: skip
        deploys.record(places, env, commit, iso(fixture_time(when, now)))
    store.write_json(
        places.plan,
        {
            "written_at": now - 180,
            "copied_at": now - 170,
            "five_hour": {"used_percentage": 62, "resets_at": now + 8040},
            "seven_day": {"used_percentage": 41, "resets_at": now + 328000},
        },
    )
    readings = [
        ("memory", "green", "11.8 GiB free", 11.8, None),
        ("cpu", "green", "34% of every CPU in use on average", 34.0, None),
        ("disk", "green", "112 GiB free of 193 GiB", 112.0, 193.0),
        ("power", "green", "on mains power", None, None),
        ("install", "green", "install 0123456789ab", None, None),
        ("hooks", "green", "the checkout runs the factory's hooks", None, None),
        ("daemon", "green", "inside the slice", None, None),
    ]
    rows = [{"name": n, "level": lv, "value": v, "figure": f, "whole": w} for n, lv, v, f, w in readings]
    merges = {"level": "green", "value": "2 waiting for their check, the oldest for 1.5 h", "waiting": 2,
              "oldest_hours": 1.5}  # fmt: skip
    store.write_json(places.vitals, {"at": iso(now), "level": "green", "why": "", "readings": rows, "merges": merges})
    use = {"worktrees": 23 * 1024**3, "docker": 38 * 1024**3, "qa": 2 * 1024**3, "transcripts": 1024**3}
    store.write_json(places.state / "gc.json", {"started": iso(now - 7200), "ended": iso(now - 7100), "failed": [],
                                                "use": use, "measured": iso(now - 7100)})  # fmt: skip
    store.write_json(
        places.state / "triage-last.json",
        {
            "at": iso(now - 7200),
            "batch": 100,
            "new": ["t-0027", "t-0028", "t-0029", "t-0030"],
            "joined": 31,
            "noise": 52,
            "parked": 13,
        },
    )
    change = {"at": iso(now - 3 * 3600), "changed": ["G2 reworded", "G5 added"], "tasks": ["t-0009"]}
    store.append_jsonl(owner.goal_changes_path(places), {**change, "requests": ["r-0041", "r-0042", "r-0043"]})


def _seed_done(places: Places, now: float) -> None:
    """What Done lists: the tasks on staging, newest merge first, on GitHub and staging and not yet on prod; and the
    branch's latest commits, the oldest from before the factory, with no task."""
    tasks, commits = [], []
    for task, count, hours in (("t-0016", 1, 2), ("t-0015", 3, 5), ("t-0013", 2, 26), ("t-0012", 4, 30)):
        at = int(now) - hours * 3600
        rows = [
            {"sha": hashlib.sha1(f"{task}-{n}".encode()).hexdigest(), "at": at - (count - n) * 600}
            | {"subject": f"Part {n} of {task}", "slice": f"{n}/{count}" if count > 1 else None}
            for n in range(count, 0, -1)
        ]
        merge = hashlib.sha1(task.encode()).hexdigest()
        reached = {"github": True, "staging": True, "prod": False}
        tasks.append({"task": task, "merge": merge, "at": at, "commits": rows, **reached})
        commits += [{**row, "task": task} for row in rows]
    first = {"sha": "0f1e2d3" + "0" * 33, "at": int(now) - 40 * 86400, "subject": "First commit", "slice": None}
    commits.append({**first, "task": None})
    head = tasks[0]["merge"]
    slot0 = {"commit": STAGING, "behind": 2, "why": None}
    listed = {"at": iso(now - 300), "local": head, "github": head, "ahead": 0, "slot0": slot0}
    store.write_json(done.path(places), {**listed, "tasks": tasks, "commits": commits})


def seed(root: Path, now: float = T0) -> Places:
    """Write the afternoon under ``root``; the warden's Places for it."""
    places = Places.under(root)
    repo = root / "repo"
    (repo / "queue").mkdir(parents=True, exist_ok=True)
    places.install.mkdir(parents=True, exist_ok=True)
    places.defaults.write_text(defaults_text(repo))
    places.state.mkdir(parents=True, exist_ok=True)
    places.totp_secret.write_text(totp.new_secret())
    # Rows per page as the UI's drives page them, the implementers' cap raised from its default, an estimate as soon
    # as a rung has one landed run, and the test address Open signs in as, to staging on the box's address.
    store.write_text(
        places.overrides,
        "[caps]\nimplementers = 4\n\n[dashboard]\npage_size = 25\n\n[routing]\n"
        "estimate_min_runs = 1\n\n"
        '[slots]\nowner_address = "owner@example.com"\nzero_origin = "http://203.0.113.7:8090"\n',
    )
    settings = settings_module.load(places.defaults, places.overrides)
    public.write_settings(places, settings)
    tasks = _seed_queue(repo, now)
    sessions = seed_sessions(now)
    store.write_text(places.history, history_lines(sessions))
    store.write_json(places.registry, {s.id: models.dump(s) for s in sessions})
    store.write_json(places.doing_of("impl-sign-in-loop"), {"qa_round": 2})
    for n, holder in ((1, "impl-sign-in-loop"), (2, "impl-retry-upload"), (3, "impl-staging-down")):
        store.write_text(
            places.slot(n) / "lease",
            f"holder={holder}\nsince={iso(now)}\n" "note=building\n",
        )
    store.write_json(places.proposals, {"open": ["t-0027", "t-0024", "t-0025"], "approved": []})
    _seed_cards(places, tasks, now)
    _seed_briefs(places, now)
    for record in seed_cost(sessions, tasks, now):
        store.append_jsonl(places.cost, models.dump(record))
    _seed_health(places, now)
    _seed_done(places, now)
    entries = []
    for e in fixture("reflect.json")["reflect"]["entries"]:
        at = iso(fixture_time(e["when"], now))
        entries.append(
            {
                "at": at,
                "kind": e["kind"],
                "who": e["who"],
                "where": e["where"],
                "text": e["text"],
                "repeats": e["repeats"],
                "last": at,
                "state": "open",
                "closed": None,
                "note": "",
                "came_back": e["came_back"],
            }
        )
    entries.sort(key=lambda e: e["at"])
    store.write_json(places.state / "reflections.json", [{"id": f"sr{n}", **e} for n, e in enumerate(entries, 1)])
    for rule, count in (("tester-time", 9), ("subagents-at-once", 6), ("git-bypass", 3)):
        for _ in range(count):
            store.append_jsonl(
                places.refusals, {"at": iso(now - 600), "session": "x", "rule": rule, "tool": "Bash", "detail": ""}
            )
    store.append_jsonl(
        places.audit,
        {
            "at": iso(now - 86400),
            "by": "owner",
            "commit": "4be21c0" + "0" * 33,
            "changes": [{"key": "caps.implementers", "was": 3, "now": 3}],
        },
    )
    seed_evidence(places, now)
    seed_check_runs(places, now)
    seed_transcript(places, now)
    # The queue is the warden's own and writable by no one else, or it refuses to read it.
    for folder in [repo, *(d for d in repo.rglob("*") if d.is_dir())]:
        os.chmod(folder, 0o755)
    devices = {hashlib.sha256(TEST_TOKEN.encode()).hexdigest(): now + 365 * 86400}
    store.write_json(places.state / "devices.json", devices)
    secrets = places.state / "secrets"
    for name, days in (("main", 9), ("work", 2)):
        path = claude_accounts.token_file(secrets, name)
        store.write_text(path, f"sk-ant-oat01-seeded-token-{name}\n", mode=0o600)
        os.utime(path, (now - days * 86400, now - days * 86400))
    store.write_text(secrets / claude_accounts.UNIT_TOKEN, "sk-ant-oat01-seeded-token-main\n", mode=0o600)
    return places


def _move(repo: Path, task_id: str, entry: dict[str, str], **fields: Any) -> models.Task:
    """``task_id`` moved to ``entry``'s status: its file, with ``fields`` changed, and its log's line."""
    path = repo / "queue" / "tasks" / f"{task_id}.md"
    task = formats.parse_task(path.read_text())
    path.write_text(formats.dump_task(dataclasses.replace(task, status=entry["to"], **fields)))
    with (repo / "queue" / "tasks" / f"{task_id}.log.jsonl").open("ab") as log:
        log.write(records.dump_log({**entry, "from": task.status}))
    return task


def _bench_for_own(places: Places, task_id: str) -> None:
    """The task's implementer gives up its place and waits, benched for its own task."""
    sessions = registry.read(places)
    for s in sessions.values():
        if s.task == task_id and s.role == "implementer" and s.state != models.RETIRED:
            s.state, s.benched_for = models.DEPRIORITIZED, task_id
    registry.write(places, sessions)


def blocked(repo: Path, places: Places, task_id: str, why: str, at: str) -> None:
    """The orchestrator's block of ``task_id``: its status, its log's line and the card for the owner's Fixed."""
    task = _move(repo, task_id, {"at": at, "actor": "orchestrator", "event": "blocked", "text": why, "to": "blocked"})
    card = models.Card("n4", "blocked", task_id, task.title, why, at)
    store.write_json(places.cards, [*store.read_json(places.cards), models.dump(card)])


def merged(repo: Path, places: Places, task_id: str, commit: str, at: str) -> None:
    """The warden found ``task_id``'s merge, ``commit``: its implementer waits, benched, for the merge's check."""
    entry = {"at": at, "actor": "warden", "event": "merged", "text": f"merged as {commit}", "to": "merged"}
    _move(repo, task_id, {**entry, "commit": commit})
    store.write_json(turn.merges_path(places), {**turn.merges(places), commit: task_id})
    _bench_for_own(places, task_id)


def saved_check(repo: Path, commit: str, at: float, results: dict[str, str]) -> None:
    """The orchestrator saved a check of ``commit``, the default branch walked there: each scenario's result."""
    name = f"c-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(at))}-{commit[:7]}"
    rows = [records.ScenarioRow(scenario, result, 3, "evidence/x") for scenario, result in results.items()]
    record = records.CheckRecord(name, commit, iso(at), "Every scenario of the default branch, walked.", rows)
    write(repo / "queue" / "checks" / f"{name}.md", records.dump_check(record))


def held(repo: Path, places: Places, task_id: str, after: list[str], at: str) -> None:
    """The orchestrator put ``task_id`` on hold until each of ``after`` passes its check; its implementer waits."""
    entry = {"at": at, "actor": "orchestrator", "event": "on-hold", "text": "Its seed comes with another task"}
    _move(repo, task_id, {**entry, "to": "on-hold"}, after=after)
    _bench_for_own(places, task_id)
