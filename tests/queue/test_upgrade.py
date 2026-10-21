"""The warden's start brings the state an earlier factory left to what this one reads, once."""

from __future__ import annotations

import dataclasses
import json

from queue_kit import World

from runtime.guard.facts import WALKED, read_ready
from runtime.qa import findings
from runtime.queue import approvals, cards, formats, intake, records, reports, turn
from runtime.queue.book import Queue, spec_rel, task_rel
from runtime.shared import store
from runtime.slots import leases
from runtime.warden import gc, inbox, loop, upgrade
from runtime.warden.context import Caller
from runtime.warden.health import errors_import
from runtime.warden.owner import reflections
from runtime.warden.sessions import doing

FULL = "f" * 40


def test_an_earlier_factorys_state_is_brought_over_once_and_reads(world: World) -> None:
    world.launcher.git = lambda argv: (0, FULL + "\n")  # rev-parse names the full commit
    places = world.places
    # The inbox: one delivered count.
    store.write_json(places.state / "inbox-cursor.json", {"delivered": 4})
    # A slot lease that names its checkout.
    store.write_text(
        places.slot(1) / "lease", "checkout=/w/impl\nholder=impl-t-0001\nsince=2026-10-09T10:00:00Z\nnote=\n"
    )
    # Prod's issues, kept by id alone, and the request glitchtip filed for one.
    store.write_json(places.state / "errors-seen.json", {"seen": ["7"], "polled": None, "failing": False, "note": None})
    queue = Queue.of(world.ctx())
    with queue.lock():
        req = intake.file_request(world.ctx(), queue, "agents", "glitchtip", "normal", "prod-app:x",
                                  "GlitchTip issue 7 in prod-app is new: KeyError")  # fmt: skip
    # A task in its merge turn, and one the old factory logged merged by a short commit.
    in_turn, merged = world.task(), world.task()
    world.write_raw(
        task_rel(in_turn.id), formats.dump_task(dataclasses.replace(in_turn, status="turn", branch="b", run=1))
    )
    queue.log(merged.id, "2026-10-09T11:00:00Z", "orchestrator", "merged", "merged as abcdef1", commit="abcdef1")
    # The loop's memo, with the request of staging's red spell; and a verdict a tester typed with one blocker.
    memo = {"probed": {}, "deployed": {}, "staging_red": "r-1", "loop_due": None, "triage_due": None, "goal": None}
    store.write_json(loop.memo_path(places), memo)
    world.session("s-impl", "implementer", "impl-t-0001", task=in_turn.id)
    typed = {"head": FULL, "tester": "qa-ux", "blockers": 1, "majors": 0, "minors": 0, "at": "x"}
    store.append_jsonl(places.results_of("s-impl"), typed)
    # An approval that names no route, beside the owner's Go on another task.
    store.write_json(places.approvals / f"{merged.id}.json", {"task": merged.id, "rung": 2, "model": "m",
                                                             "by": "owner", "at": "x", "digest": "d"})  # fmt: skip
    store.write_json(places.approvals / f"{in_turn.id}.go.json", {"task": in_turn.id, "by": "owner", "at": "x"})
    # A clean-up record that does not say what failed, and a Docker reading file.
    store.write_json(gc.record_path(places), {"last_day": "2026-10-09"})
    store.write_json(places.state / "docker-disk.json", {"at": "x", "bytes": 1, "budget_bytes": 2})
    # The owner's pause, which gives no reason.
    store.write_json(places.state / "paused.json", {"at": "x", "sessions": [], "op": "op-x"})

    ctx = dataclasses.replace(world.ctx(), caller=Caller("warden", role="warden"))
    done = upgrade.migrate(ctx)

    assert len(done) == 11, done
    assert json.loads((places.state / "paused.json").read_text())["why"] is None
    assert gc.record(places) is None and not (places.state / "docker-disk.json").exists()
    assert inbox.unacknowledged(places) == []
    assert leases.read(places, 1) is not None
    filed = json.loads(errors_import.path(places, "prod").read_text())["filed"]
    assert filed == {"7": {"request": req.id, "at": req.at}} and not (places.state / "errors-seen.json").exists()
    assert errors_import.note(places) is None  # each record reads, and none has read yet
    assert turn.holder(places) == {"task": in_turn.id, "branch": "b", "base": FULL}
    assert turn.merges(places) == {FULL: merged.id}
    assert "staging_red" not in json.loads(loop.memo_path(places).read_text())
    assert findings.open_count(ctx, in_turn.id, FULL, "qa-ux") == 1
    approval = approvals.current(places, merged.id)
    assert approval is not None and approval.routed == 2
    assert upgrade.migrate(ctx) == []  # once brought over, nothing changes


def test_a_spec_or_card_whose_brief_is_a_claude_artifact_names_no_brief_once_brought_over(world: World) -> None:
    task = world.task()
    spec = records.Spec(task.id, "G1", 4, "review", "v1", "2026-10-09T10:00:00Z", "A spec", "What changes.")
    world.write_raw(spec_rel(task.id), records.dump_spec(spec).replace("brief: v1", "brief: https://claude.ai/x"))
    card = {"id": "n1", "kind": "spec", "task": task.id, "title": "A spec", "ask": "Go?", "created": "x",
            "picture": "", "brief": "https://claude.ai/x", "comments": 3, "answered": "", "asked_by": "s",
            "comments_at": "x", "limit": ""}  # fmt: skip
    store.write_json(world.places.cards, [card])
    ctx = dataclasses.replace(world.ctx(), caller=Caller("warden", role="warden"))

    assert "1 specs and 1 cards off their artifact briefs" in upgrade.migrate(ctx)

    found = Queue.of(world.ctx()).spec(task.id)
    assert found is not None and found.brief == ""
    assert [c.brief for c in cards.read(world.places)] == [""]
    assert upgrade.migrate(ctx) == []


def test_an_errors_record_that_has_read_reads_on_and_one_that_never_read_takes_what_is_open_as_known(
    world: World,
) -> None:
    places = world.places
    for env, polled in (("prod", 1_791_000_000.0), ("slot0", None)):
        store.write_json(
            errors_import.path(places, env), {"filed": {}, "polled": polled, "failing": False, "note": None}
        )
    upgrade.migrate(dataclasses.replace(world.ctx(), caller=Caller("warden", role="warden")))
    since = {env: json.loads(errors_import.path(places, env).read_text())["since"] for env in ("prod", "slot0")}
    assert since["prod"] is not None and since["slot0"] is None


def test_an_import_timer_that_said_only_whether_it_failed_says_since_when(world: World) -> None:
    places = world.places
    store.write_json(reports.timer_path(places), {"polled": 1_791_000_000.0, "failing": True, "note": "failed"})
    store.write_json(errors_import.path(places, "prod"), {"filed": {}, "since": None, "polled": None, "failing": False,
                                                          "note": None})  # fmt: skip
    ctx = dataclasses.replace(world.ctx(), caller=Caller("warden", role="warden"))

    assert "since when reports-poll.json, errors-prod.json have failed" in upgrade.migrate(ctx)

    assert json.loads(reports.timer_path(places).read_text())["failing_since"] == ctx.clock.now()
    assert json.loads(errors_import.path(places, "prod").read_text())["failing_since"] is None
    assert reports.note(ctx) == "failed" and errors_import.note(places) is None
    assert upgrade.migrate(ctx) == []


def test_self_reflect_entries_open_under_the_ids_the_library_gave_them_by_their_place(world: World) -> None:
    old = [{"at": f"2026-10-0{n}T10:00:00Z", "kind": "miss", "who": "warden", "where": "", "text": f"Entry {n}.",
            "repeats": 1, "last": f"2026-10-0{n}T10:00:00Z"} for n in (1, 2)]  # fmt: skip
    store.write_json(reflections.records_path(world.places), old)
    ctx = dataclasses.replace(world.ctx(), caller=Caller("warden", role="warden"))

    assert "2 Self-reflect entries, each open" in upgrade.migrate(ctx)
    assert [(e["id"], e["text"], e["state"]) for e in reflections.entries(world.places)] == [
        ("sr1", "Entry 1.", "open"), ("sr2", "Entry 2.", "open")
    ]  # fmt: skip
    assert upgrade.migrate(ctx) == []


def test_a_head_a_tester_walked_to_a_verdict_before_the_upgrade_stays_a_qa_round_and_the_round_is_counted_again(
    world: World,
) -> None:
    places, heads = world.places, ["a" * 40, "b" * 40, "c" * 40, "d" * 40]
    world.session("s-impl", "implementer", "impl-t-0001")
    store.write_json(places.ready("s-impl"), {"records": [{"head": h, "slices": [], "at": "x"} for h in heads]})
    verdict = {"tester": "qa-backend", "agent": "ag", "at": "x", "blockers": 0, "majors": 1, "minors": 0}
    store.append_jsonl(places.results_of("s-impl"), {**verdict, "head": heads[0]})
    store.append_jsonl(places.results_of("s-impl"), {**verdict, "head": heads[1], "blocked": "the session was benched"})
    doing.record(places, "s-impl", qa_round=4)
    ctx = dataclasses.replace(world.ctx(), caller=Caller("warden", role="warden"))

    assert upgrade.migrate(ctx) != []

    assert [r.get(WALKED, False) for r in read_ready(places, "s-impl")] == [True, False, False, False]
    assert doing.facts(places, "s-impl")["qa_round"] == 2
    assert upgrade.migrate(ctx) == []
