"""A warden state folder and a product checkout that hold what the UI's fixtures show, in the real formats.

The fixtures (testdata/dashboard-fixtures) are the UI's picture of one busy afternoon; this module writes the same
afternoon as the warden keeps it: registry, queue files, cards, proposals, costs, probes, deploys, plan limits,
vitals and reflections. The dashboard's views are then built from it, and must give back payloads of the fixtures'
shapes.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from seed_agents import history_lines, seed_cost, seed_evidence, seed_sessions, seed_transcript
from seed_kit import FIXTURES, PRICES, ROOT, fixture, fixture_time, iso, write
from seed_queue import seed_checks, seed_requests, seed_tasks
from settings_text import set_line

from runtime.queue import formats, records
from runtime.shared import claude_accounts, models, store, totp
from runtime.shared import settings as settings_module
from runtime.shared.paths import Places
from runtime.warden import public
from runtime.warden.delivery import deploys

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
    return text.replace("[prices]", "[prices]\n" + prices.rstrip("\n"), 1)


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
            "https://claude.ai/code/artifact/00000000-0000-4000-8000-0000000" "00012",
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
                brief="https://claude.ai/code/artifact/x" if kind == "spec" else "",
                comments=3 if kind == "spec" else 0,
                asked_by="impl-retry-upload",
                comments_at=iso(now - 7200) if kind == "spec" else "",
            )
        )
    store.write_json(places.cards, [models.dump(c) for c in cards])


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
    deploys.note_branch(places, "staging", STAGING, True, now)  # as the tick notes a build on the default branch
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
        ("memory", "green", "11.8 GiB free", 11.8),
        ("cpu", "green", "pressure 34% over a minute", 34.0),
        ("power", "green", "on mains power", None),
        ("install", "green", "install 0123456789ab", None),
        ("hooks", "green", "the checkout runs the factory's hooks", None),
        ("daemon", "green", "inside the slice", None),
    ]
    rows = [{"name": n, "level": lv, "value": v, "figure": f} for n, lv, v, f in readings]
    store.write_json(places.vitals, {"at": iso(now), "level": "green", "readings": rows})
    store.write_json(
        places.state / "docker-disk.json", {"at": iso(now), "bytes": 38 * 1024**3, "budget_bytes": 60 * 1024**3}
    )
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


def seed(root: Path, now: float = T0) -> Places:
    """Write the afternoon under ``root``; the warden's Places for it."""
    places = Places.under(root)
    repo = root / "repo"
    (repo / "queue").mkdir(parents=True, exist_ok=True)
    places.install.mkdir(parents=True, exist_ok=True)
    places.defaults.write_text(defaults_text(repo))
    places.state.mkdir(parents=True, exist_ok=True)
    places.totp_secret.write_text(totp.new_secret())
    # Rows per page as the UI's drives page them, the implementers' cap raised from its default, and an estimate as
    # soon as a rung has one landed run.
    store.write_text(
        places.overrides,
        "[caps]\nimplementers = 4\n\n[dashboard]\npage_size = 25\n\n[routing]\n" "estimate_min_runs = 1\n",
    )
    settings = settings_module.load(places.defaults, places.overrides)
    public.write_settings(places, settings)
    tasks = _seed_queue(repo, now)
    sessions = seed_sessions(now)
    store.write_text(places.history, history_lines(sessions))
    store.write_json(places.registry, {s.id: models.dump(s) for s in sessions})
    store.write_json(places.state / "doing" / "impl-sign-in-loop.json", {"qa_round": 2})
    for n, holder in ((1, "impl-sign-in-loop"), (2, "impl-retry-upload"), (3, "impl-staging-down")):
        store.write_text(
            places.slot(n) / "lease",
            f"holder={holder}\nsince={iso(now)}\n" "note=building\n",
        )
    store.write_json(places.proposals, {"open": ["t-0027", "t-0024", "t-0025"], "approved": []})
    _seed_cards(places, tasks, now)
    for record in seed_cost(sessions, tasks, now):
        store.append_jsonl(places.cost, models.dump(record))
    _seed_health(places, now)
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
            }
        )
    store.write_json(places.state / "reflections.json", sorted(entries, key=lambda e: e["at"]))
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
    store.write_text(claude_accounts.folder(secrets) / claude_accounts.ACTIVE, "main\n", mode=0o600)
    return places
