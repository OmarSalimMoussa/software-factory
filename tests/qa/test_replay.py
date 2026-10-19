"""A tester's run made into its replay: steps split at the tester's marks, beats in the order they showed on screen,
no sign-in link left in its words, its pictures or its files, the trace gone; a run that cannot be replayed says why,
once; and the dashboard reads a replay without following a link."""

from __future__ import annotations

import dataclasses
import json
import logging
from pathlib import Path

import pytest
import replay_run
from queue_kit import World

from runtime.qa import evidence, retention
from runtime.qa.replay import build, pending, reader
from runtime.qa.replay.trace import ReplayError
from runtime.shared import store
from runtime.shared.protocol import Refused
from runtime.warden.sessions import registry

SESSION = "0b6f1c3e-1d2a-4c5b-9e8f-7a6b5c4d3e2f"
ENTRY = {
    "task": "t-0001",
    "head": "c" * 40,
    "tester": "qa-ux",
    "run": "20261009T120500Z-0a1b2c",
    "session": SESSION,
    "agent": "a1",
    "at": "2026-10-09T14:05:00Z",
    "files": [],
}
# Each secret as the run held it, and the links as a page address reads them.
SECRETS = (replay_run.LINK, replay_run.HANDED, replay_run.PASSWORD, "app.test/auth/link/")


def test_a_run_becomes_its_replay_split_at_the_testers_marks(tmp_path: Path) -> None:
    given = replay_run.make(tmp_path / "run", tmp_path / "agent-a1.jsonl")
    data = build.build(given)
    assert json.loads((given.run / "replay" / "replay.json").read_text()) == data
    [item] = data["items"]
    assert (item["item"], item["verdict"], item["at"], item["severity"]) == ("cart", "fail", 2, "major")
    one, two, three = item["steps"]
    assert (one["name"], one["expected"], one["result"]) == ("Open", "The cart opens", "pass")
    assert (one["got"], two["got"]) == (
        "The cart opened; the desktop took the desktop's password",
        "It stayed on the sign-in link",
    )
    assert [(b["k"], b["text"]) for b in one["beats"]] == [
        ("think", "I'll open the cart first."),
        ("go", "Opened app.test/cart"),
    ]
    # A step opens with what the tester said it would do, though it marked the step after saying it.
    assert two["beats"][0] == {"k": "think", "text": "Now I'll pay.", "at": 2.6}
    click = two["beats"][1]
    assert (click["text"], click["x"], click["y"], click["down"]) == ("Clicked “Pay now”", 640, 420, 4.2)
    assert two["finding"] == {"severity": "major", "text": "Pay now does nothing at the sign-in link"}
    # The step's screenshot was taken while the link could be on screen: its last frame before stands in.
    assert (two["still"], one["still"]) == ("replay/frames/000006.jpg", "replay/frames/000002.jpg")
    assert (three["name"], three["result"], three["beats"], three["still"]) == ("Receipt", "skip", [], None)


def test_a_steps_beats_never_go_back_in_time(tmp_path: Path) -> None:
    data = build.build(replay_run.make(tmp_path / "run", tmp_path / "agent-a1.jsonl"))
    for step in data["items"][0]["steps"]:
        shown = [b["seg"] for b in step["beats"] if b["k"] != "think"]
        assert all(later[0] >= earlier[1] for earlier, later in zip(shown, shown[1:], strict=False)), shown
    kinds = [b["k"] for b in data["items"][0]["steps"][1]["beats"]]
    assert kinds == [
        "think",
        "click",
        "type",
        "go",
        "think",
        "wait",
        "shot",
        "read",
    ]  # the screenshot after the wait it beat


def test_no_secret_is_left_in_the_replay_its_pictures_or_its_files(tmp_path: Path) -> None:
    given = replay_run.make(tmp_path / "run", tmp_path / "agent-a1.jsonl")
    data = build.build(given)
    words = [b["text"] for b in data["items"][0]["steps"][1]["beats"]]
    assert {"the sign-in link", "Opened the sign-in link", "The page still shows the sign-in link."} <= set(words)
    # From when the typing was sent until well after it ended: it may have waited behind another call.
    assert data["hidden"] == [[6.0, 6.5 + replay_run.SECRET_SECONDS]]
    assert [f["t"] for f in data["frames"]] == [0, 1, 2, 3, 4, 5]
    assert (given.run / "replay" / "frames" / "000006.jpg").read_bytes() == b"jpeg 5"
    left = sorted(str(p.relative_to(given.run)) for p in given.run.rglob("*") if p.is_file())
    assert [p for p in left if not p.startswith("replay/frames/")] == ["page-9.yml", "replay/replay.json"]
    assert not any(secret in (given.run / p).read_text(errors="replace") for p in left for secret in SECRETS)


def test_the_same_run_always_gives_the_same_replay(tmp_path: Path) -> None:
    first = build.build(replay_run.make(tmp_path / "a" / "run", tmp_path / "a" / "agent-a1.jsonl"))
    again = build.build(replay_run.make(tmp_path / "b" / "run", tmp_path / "b" / "agent-a1.jsonl"))
    assert first == again


def test_a_run_that_cannot_be_replayed_says_why_and_keeps_no_trace_nor_secret(tmp_path: Path) -> None:
    given = replay_run.make(tmp_path / "run", tmp_path / "agent-a1.jsonl")
    (given.run / "traces" / "run.trace").unlink()
    with pytest.raises(ReplayError, match="the run has 0 traces, not one"):
        build.build(given)
    build.failed(given.run, "the run has 0 traces, not one, so it cannot be replayed")
    replay = json.loads((given.run / "replay" / "replay.json").read_text())
    assert replay == {"version": 1, "error": "the run has 0 traces, not one, so it cannot be replayed"}
    assert not (given.run / "traces").exists() and not (given.run / "secrets.json").exists()
    assert not (given.run / "page-3.yml").exists() and (given.run / "page-9.yml").exists()


def test_a_step_marked_past_the_scenarios_last_still_shows_and_counts(tmp_path: Path) -> None:
    given = replay_run.make(tmp_path / "run", tmp_path / "agent-a1.jsonl")
    marks = [{"item": "cart", "step": n, "at": replay_run.iso(n)} for n in (1, 2, 3, 4)]
    ends = [{**mark, "result": "fail" if mark["step"] == 4 else "pass", "got": "seen"} for mark in marks]
    item = build.build(dataclasses.replace(given, records=marks + ends))["items"][0]
    shown = [(s["n"], s["name"], s["result"]) for s in item["steps"]]
    assert shown == [(1, "Open", "pass"), (2, "Pay", "pass"), (3, "Receipt", "pass"), (4, "", "fail")]
    assert (item["verdict"], item["at"]) == ("fail", 4)


def test_each_frame_plays_the_picture_the_trace_names_in_its_resources(tmp_path: Path) -> None:
    given = replay_run.make(tmp_path / "run", tmp_path / "agent-a1.jsonl")
    data = build.build(given)
    assert [(given.run / f["file"]).read_bytes() for f in data["frames"]] == [f"jpeg {n}".encode() for n in range(6)]


@pytest.mark.parametrize("name", ["../../../../etc/hostname", "/etc/hostname", "..", "x\\..\\..\\hostname"])
def test_a_trace_that_names_a_picture_by_a_path_is_not_replayed(tmp_path: Path, name: str) -> None:
    given = replay_run.make(tmp_path / "run", tmp_path / "agent-a1.jsonl")
    trace = given.run / "traces" / "run.trace"
    trace.write_text(trace.read_text().replace(json.dumps(replay_run.picture(3)), json.dumps(name)))
    with pytest.raises(ReplayError, match="names its picture by more than a plain name"):
        build.build(given)


def _taken(world: World, entry: dict[str, object]) -> Path:
    store.append_jsonl(evidence.index_path(world.places), entry)
    return evidence.folder(world.places, entry)


def test_the_replays_pass_builds_each_run_once_from_the_wardens_own_records(world: World, tmp_path: Path) -> None:
    world.session(SESSION, "implementer", "impl-t-0001", "t-0001", "/wt/impl-t-0001")
    parent = tmp_path / "projects" / "-wt-impl-t-0001" / f"{SESSION}.jsonl"
    parent.parent.mkdir(parents=True)
    parent.write_text("")
    given = replay_run.make(_taken(world, ENTRY), parent.with_suffix("") / "subagents" / "agent-a1.jsonl")
    for record in given.records:
        store.append_jsonl(world.places.steps_of(SESSION), record)

    assert pending.build_pending(world.ctx(), [tmp_path / "projects"]) == 1
    replay = json.loads((given.run / "replay" / "replay.json").read_text())
    assert [s["result"] for s in replay["items"][0]["steps"]] == ["pass", "fail"]  # no scenario at the head
    assert pending.build_pending(world.ctx(), [tmp_path / "projects"]) == 0


def test_a_checks_run_is_replayed_from_the_session_it_ran_in_after_that_ran_on_as_a_copy(
    world: World, tmp_path: Path
) -> None:
    before, now = SESSION, "1d4e7a90-3b2c-4f5e-8a6b-0c9d8e7f6a5b"
    world.session(now, "orchestrator", "orchestrator", worktree=str(world.repo))
    sessions = registry.read(world.places)
    sessions[now] = dataclasses.replace(sessions[now], copied_from=before)
    registry.write(world.places, sessions)
    parent = tmp_path / "projects" / "-repo" / f"{before}.jsonl"
    parent.parent.mkdir(parents=True)
    parent.write_text("")
    entry = {**{k: v for k, v in ENTRY.items() if k != "task"}, "check": "c-20261009T140500Z-ccccccc"}
    given = replay_run.make(_taken(world, entry), parent.with_suffix("") / "subagents" / "agent-a1.jsonl")

    assert pending.build_pending(world.ctx(), [tmp_path / "projects"]) == 1
    replay = json.loads((given.run / "replay" / "replay.json").read_text())
    assert replay["frames"] and replay["items"] == []  # a regression tester marks no steps


def test_a_run_the_pass_cannot_replay_says_why_logs_it_and_is_not_tried_again(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    run = _taken(world, {**ENTRY, "agent": ""})
    run.mkdir(parents=True)
    with caplog.at_level(logging.WARNING, logger="factory.warden"):
        assert pending.build_pending(world.ctx(), []) == 1
    said = json.loads((run / "replay" / "replay.json").read_text())["error"]
    assert said == "which tester walked it is not known, so its transcript cannot be found"
    assert "the replay of qa-ux run 20261009T120500Z-0a1b2c for t-0001 could not be built (ReplayError)" in caplog.text
    assert pending.build_pending(world.ctx(), []) == 0


def test_the_dashboard_reads_a_replay_by_its_run_and_never_through_a_link(world: World, tmp_path: Path) -> None:
    asked = {"replay": {"of": "t-0001", "head": "c" * 12, "tester": "qa-ux", "run": ENTRY["run"]}}
    with pytest.raises(Refused, match="has no run"):
        reader.read(world.ctx(), asked)
    run = _taken(world, ENTRY)
    with pytest.raises(Refused, match="is gone from the evidence"):
        reader.read(world.ctx(), asked)
    store.write_json(retention.cleaned_path(world.places), {"t-0001": "2026-10-09T12:00:00Z"})
    with pytest.raises(Refused, match="the QA of t-0001 was cleaned up on 2026-10-09"):
        reader.read(world.ctx(), asked)
    (run / "replay").mkdir(parents=True)
    assert reader.read(world.ctx(), asked)["replay"] is None  # not built yet
    (tmp_path / "elsewhere.json").write_text('{"version": 1, "error": "planted"}')
    (run / "replay" / "replay.json").symlink_to(tmp_path / "elsewhere.json")
    with pytest.raises(OSError):
        reader.read(world.ctx(), asked)
    (run / "replay" / "replay.json").unlink()
    (run / "replay" / "replay.json").write_text('{"version": 1, "error": "no trace"}')
    assert reader.read(world.ctx(), asked) == {"entry": ENTRY, "replay": {"version": 1, "error": "no trace"}}


def test_a_run_that_breaks_the_builder_says_so_and_the_runs_after_it_are_still_built(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    first, second = (_taken(world, {**ENTRY, "run": run}) for run in (ENTRY["run"], "20261009T120600Z-0d0e0f"))
    for run in (first, second):
        run.mkdir(parents=True)
    (first / "secrets.json").write_text("[unreadable")
    (first / "page-1.yml").write_text("- Page URL: https://app.test/anything\n")
    built: list[Path] = []

    def build_or_break(given: build.Inputs) -> dict[str, object]:
        if given.run == first:
            raise TypeError("a recorder line of the wrong kind")
        built.append(given.run)
        return {}

    monkeypatch.setattr(
        pending,
        "inputs",
        lambda ctx, roots, entry, run: dataclasses.replace(replay_run.make(run / "made", run / "t.jsonl"), run=run),
    )
    monkeypatch.setattr(pending, "build", build_or_break)
    with caplog.at_level(logging.ERROR, logger="factory.warden"):
        assert pending.build_pending(world.ctx(), []) == 2
    said = json.loads((first / "replay" / "replay.json").read_text())["error"]
    assert said == "it could not be built (TypeError)" and built == [second]
    assert "the replay of qa-ux run 20261009T120500Z-0a1b2c for t-0001 broke (TypeError)" in caplog.text
    # Its secrets could not be read, so every text file it held is gone with them.
    assert not (first / "secrets.json").exists() and not (first / "page-1.yml").exists()
