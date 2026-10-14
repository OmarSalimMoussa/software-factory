"""The warden's start brings the state an earlier factory left to what this one reads, once."""

from __future__ import annotations

import dataclasses
import json

from queue_kit import World

from runtime.qa import findings
from runtime.queue import approvals, cards, formats, intake, records, turn
from runtime.queue.book import Queue, spec_rel, task_rel
from runtime.shared import store
from runtime.slots import leases
from runtime.warden import inbox, upgrade
from runtime.warden.context import Caller
from runtime.warden.delivery import deploys, prod_push
from runtime.warden.health import errors_import, probes
from runtime.warden.owner import factory_upgrade

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
    # Prod's build, released against; and a verdict a tester typed with one blocker.
    deploys.record(places, probes.PROD, "c" * 40, "2026-10-09T09:00:00Z")
    world.session("s-impl", "implementer", "impl-t-0001", task=in_turn.id)
    typed = {"head": FULL, "tester": "qa-ux", "blockers": 1, "majors": 0, "minors": 0, "at": "x"}
    store.append_jsonl(places.state / "qa-results" / "s-impl.jsonl", typed)
    # An approval that names no route, beside the owner's Go on another task.
    store.write_json(places.approvals / f"{merged.id}.json", {"task": merged.id, "rung": 2, "model": "m",
                                                             "by": "owner", "at": "x", "digest": "d"})  # fmt: skip
    store.write_json(places.approvals / f"{in_turn.id}.go.json", {"task": in_turn.id, "by": "owner", "at": "x"})

    ctx = dataclasses.replace(world.ctx(), caller=Caller("warden", role="warden"))
    done = upgrade.migrate(ctx)

    assert len(done) == 8, done
    assert inbox.unacknowledged(places) == []
    assert leases.read(places, 1) is not None
    filed = json.loads(errors_import.path(places, "prod").read_text())["filed"]
    assert filed == {"7": {"request": req.id, "at": req.at}} and not (places.state / "errors-seen.json").exists()
    assert turn.holder(places) == {"task": in_turn.id, "branch": "b", "base": FULL}
    assert turn.merges(places) == {FULL: merged.id}
    assert prod_push.released(places) == "c" * 40
    assert findings.open_count(ctx, in_turn.id, FULL, "qa-ux") == 1
    approval = approvals.current(places, merged.id)
    assert approval is not None and approval.routed == 2
    assert upgrade.migrate(ctx) == []  # once brought over, nothing changes


def test_a_release_offer_that_names_no_install_is_asked_for_again(world: World) -> None:
    path = factory_upgrade.offer_path(world.places)
    store.write_json(path, {"available": None, "checked_at": 1.0, "note": None})
    ctx = dataclasses.replace(world.ctx(), caller=Caller("warden", role="warden"))

    assert "the release offer, asked again" in upgrade.migrate(ctx)
    assert not path.exists() and upgrade.migrate(ctx) == []


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
