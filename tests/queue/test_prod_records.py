"""`factory records`: an implementer or a designer reads one file, or one folder level, of prod's records inside the
folders the product's factory.toml names, and each read is a line in its task's log naming the path alone."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest
from queue_kit import World

from runtime.shared.protocol import Refused
from runtime.warden.context import Context
from runtime.warden.health import prod_records

ROOT = Path(__file__).resolve().parents[2]
RECORDS = '\n[records]\nfolders = ["job-store/*/steps"]\n'
STEPS = "job-store/j1/steps"
ANSWER = '{"answer": "bonjour"}'


def prod(world: World, records: str = RECORDS) -> Path:
    """The fixture product's factory.toml in the checkout, and prod's job-store volume holding one job."""
    toml = (ROOT / "testdata" / "fixture-product" / "factory.toml").read_text()
    (world.repo / "factory.toml").write_text(toml + records)
    data = world.places.docker_volumes / "notes-prod_job-store" / "_data"
    for at, step in enumerate(("01-build", "02-shape", "03-translate")):
        (data / "j1/steps" / step).mkdir(parents=True)
        (data / "j1/steps" / step / "result.json").write_text(ANSWER)
        os.utime(data / "j1/steps" / step, (at, at))
    (data / "j1/input.txt").write_text("what the person said")
    return data


def reader(world: World) -> tuple[Context, str]:
    task = world.task()
    world.session("s-impl", "implementer", "implementer-t-0001", task=task.id)
    return world.ctx(role="implementer", session="s-impl"), task.id


def test_a_record_is_read_whole_and_a_folder_lists_its_newest_entries(world: World) -> None:
    prod(world)
    ctx, _ = reader(world)
    world.with_settings(telemetry__max_items=2)

    assert prod_records.records(ctx, {"path": f"{STEPS}/03-translate/result.json"}, None) == {
        "ok": True, "message": ANSWER
    }  # fmt: skip
    listed = prod_records.records(ctx, {"path": STEPS}, None)
    assert [(e["name"], e["kind"]) for e in listed["entries"]] == [("03-translate", "folder"), ("02-shape", "folder")]
    assert listed["more"] is True
    # Above a declared folder, a listing names only what leads into one: never the person's own words beside it.
    assert [e["name"] for e in prod_records.records(ctx, {"path": "job-store/j1"}, None)["entries"]] == ["steps"]
    assert prod_records.records(ctx, {}, None) == {"ok": True, "folders": ["job-store/*/steps"]}


def test_each_read_is_a_line_in_the_readers_task_log_naming_the_path_and_never_the_content(world: World) -> None:
    prod(world)
    ctx, task = reader(world)
    path = f"{STEPS}/03-translate/result.json"

    prod_records.records(ctx, {"path": path}, None)
    prod_records.records(ctx, {"path": STEPS}, None)

    logged = [e for e in world.queue().read_log(task) if e["event"] == prod_records.EVENT]
    assert [(e["actor"], e["text"]) for e in logged] == [
        ("implementer-t-0001", f"implementer-t-0001 read prod's record {path}"),
        ("implementer-t-0001", f"implementer-t-0001 listed prod's records in {STEPS}"),
    ]
    assert "bonjour" not in json.dumps(logged)


def _link_out(world: World, data: Path) -> None:
    (data / "j1/steps/out").symlink_to(world.repo)


def _link_file(world: World, data: Path) -> None:
    (data / "j1/steps/goal.md").symlink_to(world.repo / "GOAL.md")


def _large(world: World, data: Path) -> None:
    (data / "j1/steps/big.json").write_text("x" * (world.settings.get_int("telemetry.max_kib") * 1024 + 1))


def _binary(world: World, data: Path) -> None:
    (data / "j1/steps/frame.jpg").write_bytes(b"\xff\xd8\xff\xe0\x00")


@pytest.mark.parametrize(
    ("records", "path", "arrange", "said"),
    [
        ("", f"{STEPS}/01-build/result.json", None, "names no folder of prod's records"),
        (RECORDS, f"{STEPS}/../input.txt", None, "is not a path inside prod's records"),
        (RECORDS, "/etc/passwd", None, "is not a path inside prod's records"),
        (RECORDS, "job-store/j1/input.txt", None, "is outside the folders"),
        (RECORDS, f"{STEPS}/out/GOAL.md", _link_out, "is a link"),
        (RECORDS, f"{STEPS}/goal.md", _link_file, "is a link"),
        (RECORDS, f"{STEPS}/big.json", _large, "larger than"),
        (RECORDS, f"{STEPS}/frame.jpg", _binary, "is not text"),
        (RECORDS, "other-store/j1/steps/x.json", None, "is outside the folders"),
        ('\n[records]\nfolders = ["other-store"]\n', "other-store/x.json", None, "prod has no volume other-store"),
    ],
    ids=["nothing-declared", "dot-dot", "absolute", "outside", "link-on-the-way", "link", "size-cap", "binary",
         "other-volume", "no-volume"],
)  # fmt: skip
def test_a_read_stays_inside_the_declared_folders_and_under_the_size_cap(
    world: World, records: str, path: str, arrange: Callable[[World, Path], None] | None, said: str
) -> None:
    data = prod(world, records)
    if arrange is not None:
        arrange(world, data)
    ctx, _ = reader(world)
    with pytest.raises(Refused) as refused:
        prod_records.records(ctx, {"path": path}, None)
    assert said in refused.value.reason and refused.value.rule == "records"
