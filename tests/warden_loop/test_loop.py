from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest
from loop_fakes import World, healthy

from runtime.queue import formats
from runtime.shared import network, store
from runtime.shared.models import INBOXES, Request
from runtime.warden import inbox, loop
from runtime.warden.delivery import deploys
from runtime.warden.health import errors_import, probes, telemetry
from runtime.warden.health.probes import Answer

ROOT = Path(__file__).resolve().parents[2]


def kernel(tmp_path: Path) -> tuple[Path, Path]:
    proc, power = tmp_path / "proc", tmp_path / "power" / "AC"
    (proc / "pressure").mkdir(parents=True, exist_ok=True)
    (proc / "meminfo").write_text("MemAvailable:   16000000 kB\n")
    (proc / "pressure" / "cpu").write_text("some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
    power.mkdir(parents=True, exist_ok=True)
    (power / "type").write_text("Mains\n")
    (power / "online").write_text("1\n")
    return proc, power.parent


def file_requests(world: World, count: int, age_hours: float) -> None:
    queue = world.checkout / "queue"
    for folder in [queue, queue / "requests", *(queue / "requests" / box for box in INBOXES)]:
        folder.mkdir(parents=True, exist_ok=True)
        os.chmod(folder, 0o755)
    at = formats.iso(world.clock.now() - age_hours * 3600)
    for n in range(1, count + 1):
        request = Request(
            f"r-2026-10-01-{n:04d}", "agents", "implementer", at, "normal", "the sign-in page", "It fails."
        )
        (queue / "requests" / "agents" / f"{request.id}.md").write_text(formats.dump_request(request))


def run(world: World, tmp_path: Path) -> None:
    loop.run_tick(world.ctx, lambda url, timeout: healthy("a" * 40), *kernel(tmp_path))


def tick(world: World, tmp_path: Path) -> list[str]:
    run(world, tmp_path)
    return [e["kind"] for e in inbox.read(world.ctx, {}, None)["events"]]


@pytest.fixture
def ticking(world: World, github_repo: Path) -> World:
    """A world whose every tick step can run: its checkout is a git repo the warden's mirror can read."""
    world.ctx.post_event = inbox.poster(world.ctx.places, world.clock)
    world.checkout.mkdir(parents=True, exist_ok=True)
    (world.checkout / "GOAL.md").write_bytes((ROOT / "testdata" / "fixture-product" / "GOAL.md").read_bytes())
    small = world.ctx.settings().with_changes({"intake.batch_size": 10, "intake.batch_max_wait_hours": 6})
    world.ctx.settings = lambda: small
    return world


@pytest.mark.parametrize(
    ("count", "age_hours", "due"),
    [(10, 0, True), (9, 0, False), (1, 6, True), (1, 5.9, False)],
    ids=["batch-full", "batch-short", "oldest-waited-long-enough", "oldest-not-yet"],
)
def test_triage_is_due_at_the_batch_size_or_the_longest_wait(
    ticking: World, tmp_path: Path, count: int, age_hours: float, due: bool
) -> None:
    file_requests(ticking, count, age_hours)
    assert (inbox.TRIAGE_DUE in tick(ticking, tmp_path)) is due


def test_loop_due_comes_once_per_loop_period_after_the_last_pass(ticking: World, tmp_path: Path) -> None:
    file_requests(ticking, 0, 0)
    run(ticking, tmp_path)
    ticking.clock.t += 60
    assert tick(ticking, tmp_path) == [inbox.LOOP_DUE]  # the second tick added no copy
    loop.tick(ticking.ctx, {}, None)
    period = ticking.ctx.settings().get_int("watch.loop_minutes") * 60
    ticking.clock.t += period - 1
    assert tick(ticking, tmp_path) == []
    ticking.clock.t += 1
    assert tick(ticking, tmp_path) == [inbox.LOOP_DUE]


def test_a_failed_errors_read_never_stops_the_loop_and_is_logged_once_a_run(
    ticking: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    file_requests(ticking, 0, 0)
    store.write_json(telemetry.server_json(ticking.ctx.places), {
        "glitchtip": {"url": "https://box.test:8200", "org": "notes", "token": "tok-read"},
        "langfuse": {"url": "https://box.test:3100", "public_key": "pk-lf-1", "secret_key": "sk-lf-1"},
    })  # fmt: skip
    reads: list[str] = []

    def down(url: str, headers: dict[str, str], timeout: float) -> network.Answer:
        reads.append(url)
        raise TimeoutError(url)

    monkeypatch.setattr(telemetry, "http_get", down)
    period = ticking.ctx.settings().get_int("errors.poll_minutes") * 60
    events = []
    with caplog.at_level(logging.WARNING, logger=errors_import.__name__):
        for _ in range(2):
            events += tick(ticking, tmp_path)
            ticking.clock.t += period
    assert len(reads) == 4 and inbox.LOOP_DUE in events  # read again at the next poll; the tick went on
    assert [r.getMessage() for r in caplog.records] == [
        f"warden: importing {env}'s errors failed (GlitchTip did not answer /api/0/organizations/notes/issues/ "
        "(TimeoutError)); it runs again at the next poll"
        for env in ("prod", "staging")
    ]
    assert "(TimeoutError)" in str(errors_import.note(ticking.ctx.places))


def test_a_failing_tick_step_is_logged_and_the_steps_after_it_still_run(
    ticking: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    file_requests(ticking, 0, 0)

    def broken(*args: object) -> None:
        raise OSError("no /proc")

    monkeypatch.setattr(loop.vitals, "refresh", broken)
    with caplog.at_level(logging.ERROR, logger=loop.__name__):
        events = tick(ticking, tmp_path)
        tick(ticking, tmp_path)  # it fails the same way again: reported once while it lasts
    assert inbox.LOOP_DUE in events
    assert [r.getMessage() for r in caplog.records] == ["warden: the tick's vitals step failed (OSError)"]


def _staging_fails(world: World, tmp_path: Path, times: int) -> list[str]:
    """Staging fails ``times`` probes in a row, a probe period apart; the events of the last tick."""
    kinds: list[str] = []
    for _ in range(times):
        world.clock.t += world.ctx.settings().get_int("watch.probe_seconds")
        loop.run_tick(world.ctx, lambda url, timeout: Answer(502, b"") if "staging" in url else healthy("a" * 40),
                      *kernel(tmp_path))  # fmt: skip
        kinds = [e["kind"] for e in inbox.read(world.ctx, {}, None)["events"]]
    return kinds


def test_staging_goes_red_after_failures_in_a_row_and_never_while_a_push_deploys_it(
    ticking: World, tmp_path: Path
) -> None:
    file_requests(ticking, 0, 0)
    needed = ticking.ctx.settings().get_int("watch.outage_after")
    deploys.expect(ticking.ctx.places, probes.STAGING, "c" * 40, ticking.clock.now(), 30)
    assert inbox.STAGING_RED not in _staging_fails(ticking, tmp_path, needed + 2)  # the deploy's restart
    deploys.answered(ticking.ctx.places, probes.STAGING, "c" * 40)  # it landed, then broke

    ticking.clock.t += ticking.ctx.settings().get_int("watch.probe_seconds")
    loop.run_tick(ticking.ctx, lambda url, timeout: healthy("c" * 40), *kernel(tmp_path))
    assert inbox.STAGING_RED not in _staging_fails(ticking, tmp_path, needed - 1)
    assert inbox.STAGING_RED in _staging_fails(ticking, tmp_path, 1)
    red = next(e for e in inbox.read(ticking.ctx, {}, None)["events"] if e["kind"] == inbox.STAGING_RED)
    assert red["data"]["suspect"] == "c" * 40 and red["data"]["request"].startswith("r-")


def test_a_read_only_walk_of_prod_is_asked_for_a_day_after_the_last_one(ticking: World, tmp_path: Path) -> None:
    file_requests(ticking, 0, 0)
    run(ticking, tmp_path)  # the first tick starts the count
    ticking.clock.t += ticking.ctx.settings().get_int("qa.prod_every_hours") * 3600

    run(ticking, tmp_path)

    due = [e for e in inbox.unacknowledged(ticking.ctx.places) if e["kind"] == inbox.QA_PROD_DUE]
    assert [e["data"]["kind"] for e in due] == ["daily"]


def test_every_waiting_task_is_scored_again_together_on_its_schedule(
    ticking: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_requests(ticking, 0, 0)
    rescored: list[float] = []
    real = loop.tasks.rescore
    monkeypatch.setattr(loop.tasks, "rescore", lambda ctx: rescored.append(ctx.clock.now()) or real(ctx))
    every = ticking.ctx.settings().get_int("intake.rescore_hours") * 3600

    start = ticking.clock.t
    run(ticking, tmp_path)  # the goal step's first reading of GOAL.md scores them too
    ticking.clock.t += every - 1
    run(ticking, tmp_path)
    ticking.clock.t += 1
    run(ticking, tmp_path)

    assert sorted(set(rescored)) == [start, start + every]


def test_a_boosted_request_is_posted_for_triage_at_once_and_once(ticking: World, tmp_path: Path) -> None:
    file_requests(ticking, 1, 0)
    store.write_json(ticking.ctx.places.state / "request-boosts.json", {"r-2026-10-01-0001": ticking.clock.iso()})
    posted = []
    for _ in range(3):  # each pass the orchestrator reads its inbox and acknowledges with its tick
        posted += [k for k in tick(ticking, tmp_path) if k == inbox.TRIAGE_DUE]
        loop.tick(ticking.ctx, {}, None)
        ticking.clock.t += ticking.ctx.settings().get_int("watch.tick_seconds")

    assert posted == [inbox.TRIAGE_DUE]
