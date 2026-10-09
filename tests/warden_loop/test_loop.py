from __future__ import annotations

import os
from pathlib import Path

import pytest
from loop_fakes import World, healthy

from runtime.queue import formats
from runtime.shared.models import INBOXES, Request
from runtime.warden import inbox, loop

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
def ticking(world: World) -> World:
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
