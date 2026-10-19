"""Every page the server builds from real state has the shape the UI's fixtures have: the same keys, the same kinds
of values, at every depth. Unknown values (None) may stand anywhere."""

from __future__ import annotations

import dataclasses
import json
import tomllib
from pathlib import Path
from typing import Any

import pytest
from dashboard_rig import Rig
from dashboard_seed import STAGING, T0
from seed_agents import CHECKED, REPLAY_URL
from seed_kit import FIXTURES, iso

from runtime.dashboard.views import agents, ops, spend
from runtime.dashboard.views.kit import PartError, Snap
from runtime.dashboard.views.tasks.task import qa_line
from runtime.dashboard.views.words import agentwords, taskwords
from runtime.dashboard.views.words.settingwords import LABELS
from runtime.qa import retention
from runtime.queue import formats
from runtime.shared import models, store
from runtime.shared import operations as kinds
from runtime.shared import settings as settings_module
from runtime.shared.models import OPEN_SEVERITIES
from runtime.shared.settings_schema import SCHEMA

BROKEN = {"broken", "id", "error"}


def _pick(mine: dict[str, Any], candidates: list[Any]) -> Any:
    """The fixture row to compare a row with: the one it matches best (rows differ by kind, control or origin)."""
    return min(candidates, key=lambda c: len(shape(mine, c)))


def shape(mine: Any, theirs: Any, where: str = "") -> list[str]:
    if mine is None or theirs is None:
        return []
    if isinstance(theirs, bool | int | float | str) or isinstance(mine, bool):
        return _scalar_shape(mine, theirs, where)
    if isinstance(theirs, list):
        return _list_shape(mine, theirs, where)
    if not isinstance(mine, dict):
        return [f"{where}: want an object"]
    want = {k for k in theirs if not k.startswith("_")}
    if set(mine) != want:
        return [f"{where}: missing {sorted(want - set(mine))}, extra {sorted(set(mine) - want)}"]
    return [e for k in want for e in shape(mine[k], theirs[k], f"{where}.{k}")]


def _scalar_shape(mine: Any, theirs: Any, where: str) -> list[str]:
    if isinstance(theirs, bool) or isinstance(mine, bool):
        return [] if isinstance(mine, bool) and isinstance(theirs, bool) else [f"{where}: {mine!r} vs {theirs!r}"]
    if isinstance(theirs, int | float):
        return [] if isinstance(mine, int | float) else [f"{where}: want a number, got {mine!r}"]
    return [] if isinstance(mine, str) else [f"{where}: want text, got {mine!r}"]


def _list_shape(mine: Any, theirs: list[Any], where: str) -> list[str]:
    if not isinstance(mine, list):
        return [f"{where}: want a list"]
    candidates = [t for t in theirs if not (isinstance(t, dict) and t.get("broken"))]
    if len({type(t) for t in candidates}) > 1:  # a tuple, such as ["tasks", "Tasks", 14]: position by position
        return [e for i, (m, t) in enumerate(zip(mine, theirs, strict=False)) for e in shape(m, t, f"{where}[{i}]")]
    errors: list[str] = []
    for i, row in enumerate(mine):
        if isinstance(row, dict) and row.get("broken"):
            errors += [] if set(row) == BROKEN else [f"{where}[{i}]: a broken row is {sorted(BROKEN)}"]
        elif candidates:
            errors += shape(row, _pick(row, candidates) if isinstance(row, dict) else candidates[0], f"{where}[{i}]")
    return errors


def _fixture(name: str) -> dict[str, Any]:
    raw: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    if name.startswith("agent/") and "agent" in raw:  # what the panel gained after the fixtures were drawn
        raw["agent"].update(thread=[], message=None, chat_note=None, waits=[])
    if name == "settings.json":  # a row's control decides its value's kind, whatever its group
        every = [row for group in raw["settings"]["groups"] for row in group["rows"]]
        raw["settings"]["groups"] = [{**g, "rows": every} for g in raw["settings"]["groups"]]
    return raw


PAGES = [
    ("/api/shell", ["shell.json"]),
    ("/api/home", ["home.json"]),
    ("/api/work?tab=tasks", ["work-tasks.json"]),
    ("/api/work?tab=requests", ["work-requests.json"]),
    ("/api/work?tab=parked", ["work-parked.json"]),
    ("/api/work?tab=checks", ["work-checks.json"]),
    ("/api/work?tab=done", ["work-done.json"]),
    ("/api/work?tab=done&view=commits", ["work-done-commits.json"]),
    ("/api/task/t-0017", ["task/t-0017.json"]),
    ("/api/task/t-0027", ["task/t-0027.json"]),
    ("/api/task/t-0013", ["task/t-0013.json"]),
    ("/api/agents", ["agents.json"]),
    ("/api/agent/orchestrator", ["agent/orchestrator.json"]),
    ("/api/agent/impl-project-names", ["agent/impl-project-names.json"]),
    ("/api/agent/impl-sign-in-loop/stop-preview", ["agent/impl-sign-in-loop-stop-preview.json"]),
    ("/api/history/h1", ["history/h1.json"]),
    ("/api/spend?range=14&slice=family", ["spend.json", "spend-range-14.json", "spend-slice-family.json"]),
    ("/api/spend?range=7&slice=agent", ["spend-range-7.json", "spend-slice-agent.json"]),
    ("/api/spend?range=1&slice=model", ["spend-range-1.json", "spend-slice-model.json"]),
    ("/api/spend?range=14&slice=task", ["spend-slice-task.json"]),
    ("/api/doc/t-0017", ["doc/t-0017.json"]),
    ("/api/doc/s-0012", ["doc/s-0012.json"]),
    ("/api/doc/GOAL.md", ["doc/GOAL.md.json"]),
    ("/api/doc/c-20261009T140500Z-a1b2c3d", ["doc/c-20261009T140500Z-a1b2c3d.json"]),
    ("/api/doc/sr1", ["doc/sr1.json"]),
    ("/api/settings", ["settings.json"]),
    ("/api/reflect", ["reflect.json"]),
    ("/api/guard", ["guard.json"]),
    ("/api/push", ["push.json"]),
    (REPLAY_URL, ["replay.json"]),
    ("/api/brief/t-0023", ["brief.json"]),
]


@pytest.mark.parametrize(("url", "fixtures"), PAGES)
def test_every_page_has_the_fixture_shapes(rig: Rig, url: str, fixtures: list[str]) -> None:
    parts = rig.parts(url)
    failed = {name: part["error"] for name, part in parts.items() if not part["ok"]}
    assert not failed
    for name in fixtures:
        for part, theirs in _fixture(name).items():
            assert part in parts, f"{url} has no {part}"
            assert shape(parts[part]["data"], theirs, part) == []


def test_a_file_found_by_search_carries_the_look_of_its_kind(rig: Rig) -> None:
    found = rig.parts("/api/search?q=upload")["search"]["data"]
    tasks = next(group["items"] for group in found["groups"] if group["label"] == "Tasks")
    assert tasks[0]["icon"] == {"kind": "ktile", "key": "queue-list", "tone": "info"}


def test_a_daily_budget_far_above_the_spend_is_named_beside_the_chart_not_drawn_on_it() -> None:
    defaults = tomllib.loads((Path(__file__).resolve().parents[2] / "settings.toml").read_text())
    today = iso(T0)[:10]
    cost = {"days": {today: {"family": {"impl": [12.0, 0]}}}, "totals": {}, "tokens": {"days": {}, "task": {}}}
    snap = Snap(
        T0,
        {"cost": cost},
        {},
        settings_module.build(defaults, {"cost.daily_budget_usd": 10000, "repo.path": "/r", "repo.github": "o/n"}),
    )

    chart = spend.chart(snap, {"range": "1"})

    # A $10,000 line on the chart's scale flattened every day's bar.
    assert (chart["budget"], chart["budget_label"]) == (None, "Daily budget $10,000, above the chart")
    assert chart["max"] < 100


@pytest.mark.parametrize(
    ("paused", "icon"), [("2026-10-09T00:00:00Z", "pause-circle"), (None, "stop-circle")], ids=["paused", "stopped"]
)
def test_the_agents_banner_carries_the_icon_of_the_factorys_state(paused: str | None, icon: str) -> None:
    snap = Snap(0.0, {"factory": {"paused": paused}, "sessions": {}}, {})

    assert agents.banner(snap)["icon"] == icon


@pytest.mark.parametrize(("agent", "icon"), [("impl-project-names", "clock"), ("qa-sec-1", "lock-closed")])
def test_an_agent_out_of_the_work_says_so_with_its_icon(rig: Rig, agent: str, icon: str) -> None:
    assert rig.parts(f"/api/agent/{agent}")["agent"]["data"]["life_note"]["icon"] == icon


@pytest.mark.parametrize(
    ("benched_for", "status", "says"),
    [
        ("t-0002", "merged", "Pushed out for “Task two” · comes back first"),
        ("t-0001", "merged", "Merged · ends when its check passes, comes back if it fails"),
        ("t-0001", "blocked", "Blocked · comes back when you mark it fixed"),
        ("t-0001", "on-hold", "On hold · comes back when the task it waits for passes its check"),
        ("t-0001", "building", "Sent back to it · comes back when a place frees"),
    ],
    ids=["for-another", "merged", "blocked", "on-hold", "sent-back"],
)
def test_a_benched_agent_says_what_it_waits_for(benched_for: str, status: str, says: str) -> None:
    tasks = [{"id": "t-0001", "title": "Task one", "status": status}, {"id": "t-0002", "title": "Task two"}]
    row = {"state": models.DEPRIORITIZED, "benched_for": benched_for, "task": "t-0001"}

    assert agentwords.doing(Snap(0.0, {"queue": {"tasks": tasks}}, {}), row) == says


_CLEAN = {f"{severity}s": 0 for severity in OPEN_SEVERITIES}


@pytest.mark.parametrize(
    ("names", "results", "tone", "shots"),
    [
        ([], {}, None, []),
        (["qa-backend"], {"qa-backend": [_CLEAN]}, "good", []),
        (["qa-visual", "qa-ux"], {"qa-visual": [_CLEAN], "qa-ux": [{**_CLEAN, "majors": 1}]}, None, ["good", "bad"]),
        # Two testers of one kind, each in its own browser lane: the kind fails when either found a major.
        (["qa-ux"], {"qa-ux": [{**_CLEAN, "majors": 1}, _CLEAN]}, None, ["bad"]),
    ],
    ids=["untested", "backend-tested", "screenshots", "two-lanes"],
)
def test_a_fixs_qa_line_carries_its_tone_and_each_screenshot_its_verdicts(
    names: list[str], results: dict[str, list[dict[str, Any]]], tone: str | None, shots: list[str]
) -> None:
    line = qa_line(names, results, [f"{name}\t/api/picture/qa/{name}.png" for name in names])

    assert (line["tone"], [shot["tone"] for shot in line["shots"]]) == (tone, shots)


@pytest.mark.parametrize(
    ("state", "look"),
    [
        ("waiting", {"cls": "", "mark": "ring"}),
        ("running", {"cls": "on", "mark": "spin"}),
        ("done", {"cls": "done", "mark": "check"}),
        ("failed", {"cls": "failed", "mark": "x-mark"}),
    ],
)
def test_each_step_of_an_operation_says_how_the_page_draws_it(state: str, look: dict[str, str]) -> None:
    op = {"id": "op-1", "kind": "account", "state": "running", "result": {}, "steps": [
        {"text": "Switch the account", "state": state, "detail": ""}
    ]}  # fmt: skip
    assert ops.operation({"op": op})["steps"][0]["look"] == look


# What each kind records as it finishes, where the dashboard's words read it.
_RECORDED = {
    kinds.PUSH: {"sha": "abc1234def"},
    kinds.PUSH_BACK: {"sha": "abc1234def"},
    kinds.ACCOUNT: {"name": "work"},
    kinds.MESSAGE: {"revived": False},
    kinds.UPGRADE: {"to": "abc1234def"},
}


@pytest.mark.parametrize("kind", kinds.KINDS)
def test_every_kind_of_operation_has_a_title_and_says_what_it_did(kind: str) -> None:
    op = {"id": "op-1", "kind": kind, "state": "done", "result": _RECORDED.get(kind, {}), "steps": []}
    shown = ops.operation({"op": op})
    assert shown["title"] and shown["result"]["message"] and shown["result"]["refresh"]


def test_an_operation_the_dashboard_does_not_know_is_refused() -> None:
    op = {"id": "op-1", "kind": "unheard-of", "state": "running", "result": {}, "steps": []}
    with pytest.raises(PartError, match="does not know"):
        ops.operation({"op": op})


def test_the_outage_band_has_the_fixture_shape(rig: Rig) -> None:
    store.write_json(
        rig.places.state / "outage.json",
        {
            "since": "2026-10-09T11:30:00Z",
            "stage": "fixing",
            "task": "t-0021",
            "session": "impl-staging-down",
            "bypass_used": True,
        },
    )
    band = rig.parts("/api/home")["outage"]["data"]
    assert shape(band, _fixture("home@outage.json")["outage"], "outage") == []
    assert band["actions"] == ["watch", "stop"] and "<b>Staging is down after the last deploy</b>" in band["message"]


def test_settings_cover_every_key_of_the_schema(rig: Rig) -> None:
    rows = [r["key"] for g in rig.parts("/api/settings")["settings"]["data"]["groups"] for r in g["rows"]]
    assert sorted(rows) == sorted(SCHEMA) == sorted(LABELS)


def test_a_change_to_a_setting_the_factory_has_taken_out_stays_in_the_history(rig: Rig) -> None:
    change = {"key": "install.gone_hours", "was": 24, "now": 0}
    store.append_jsonl(rig.places.audit, {"at": iso(T0), "by": "owner", "changes": [change]})

    assert rig.parts("/api/settings")["settings"]["data"]["audit"][0]["what"] == "install.gone_hours 24 → 0"


def test_a_file_that_cannot_be_read_fails_only_its_own_part(rig: Rig) -> None:
    rig.places.cards.write_text("{not json")
    parts = rig.parts("/api/home")
    assert not parts["needs"]["ok"] and "could not read" in parts["needs"]["error"]
    assert all(part["ok"] for name, part in parts.items() if name != "needs")


def test_a_history_line_that_cannot_be_read_is_one_failed_row(rig: Rig) -> None:
    rows = rig.parts("/api/agents")["history"]["data"]["rows"]
    failed = [row for row in rows if row.get("broken")]
    assert failed == [{"broken": True, "id": "history line 6", "error": "line 6 of history.jsonl cannot be read"}]
    assert len(rows) == 10 and all(row.get("why") for row in rows if not row.get("broken"))


@pytest.mark.parametrize(
    ("prod", "runs"),
    [(None, False), ("staging", True), ("down", False)],
    ids=["an-older-build", "staging-build", "never-answered"],
)
def test_the_push_sheet_says_prod_runs_staging_only_when_prod_answers_on_stagings_build(
    rig: Rig, tmp_path: Path, prod: str | None, runs: bool
) -> None:
    if prod is not None:
        ok = prod == "staging"
        probe = models.Probe(iso(rig.clock.now()), "prod", ok, STAGING if ok else "", 80, "" if ok else "refused", True)
        store.append_jsonl(rig.places.probes, models.dump(probe))
    # Like a product just moved onto the factory: no task waits on staging.
    for task in (tmp_path / "repo" / "queue" / "tasks").glob("*.md"):
        task.unlink()

    assert rig.parts("/api/push")["push"]["data"]["prod_runs_staging"] is runs


@pytest.mark.parametrize(("ok", "says"), [(True, None), (False, "staging is red (HTTP 502)")], ids=["green", "red"])
def test_the_push_sheet_says_why_the_warden_would_refuse_the_push(rig: Rig, ok: bool, says: str | None) -> None:
    probe = models.Probe(iso(rig.clock.now()), "staging", ok, STAGING if ok else "", 80, "" if ok else "HTTP 502")
    store.append_jsonl(rig.places.probes, models.dump(probe))
    blocked = rig.parts("/api/push")["push"]["data"]["blocked"]
    assert blocked is None if says is None else says in blocked


def test_a_task_on_hold_counts_as_building_on_home_and_says_it_waits_for_another(rig: Rig, tmp_path: Path) -> None:
    def stages() -> dict[str, int]:
        return {s["key"]: s["count"] for s in rig.parts("/api/home")["tiles"]["data"]["work"]["stages"]}

    before = stages()
    texts = [p.read_text() for p in (tmp_path / "repo" / "queue" / "tasks").glob("*.md")]
    building = formats.parse_task(next(text for text in texts if "\nstatus: building\n" in text))
    held = dataclasses.replace(building, status="on-hold")
    (tmp_path / "repo" / "queue" / "tasks" / f"{held.id}.md").write_text(formats.dump_task(held))

    assert stages() == before
    word = {"tone": "ring", "word": "Waiting for another task's check to pass"}
    assert taskwords.status(Snap(0.0, {}, {}), {"id": held.id, "status": "on-hold"}) == word


def test_home_draws_every_live_proposal_even_one_a_crash_left_without_its_place(rig: Rig) -> None:
    store.write_json(rig.places.proposals, {"open": ["t-0027", "t-0024", None], "approved": []})

    slots = rig.parts("/api/home")["proposals"]["data"]["slots"]

    assert [s["task"] for s in slots if s.get("task")] == ["t-0027", "t-0024", "t-0025"]


@pytest.mark.parametrize("yolo", [False, True], ids=["owner-approves", "yolo-starts-them"])
def test_under_yolo_a_proposal_asks_no_approval_and_says_it_starts_when_a_place_is_free(rig: Rig, yolo: bool) -> None:
    if yolo:
        store.write_json(rig.places.state / "yolo.json", {"since": iso(T0), "told": []})

    found = rig.parts("/api/home")["proposals"]["data"]

    autos = {s["auto"] for s in found["slots"] if s["kind"] == "card"}
    assert autos == {"Starts when a place is free" if yolo else None}
    assert ("YOLO mode starts each one" in found["hint"]) is yolo


def test_spend_of_unknown_price_is_unknown_and_the_budget_says_it_stops_nothing(rig: Rig) -> None:
    rows = store.read_jsonl(rig.places.cost)
    store.write_text(rig.places.cost, "".join(json.dumps({**r, "usd": None}) + "\n" for r in rows))

    spend = rig.parts("/api/spend?range=7&slice=family")
    hero, days = spend["hero"]["data"], spend["chart"]["data"]["days"]
    assert hero["total"] is None and hero["budget_pct"] is None and "stops nothing" in hero["today"]
    assert {"label": "Per day, average", "value": "unknown"} in hero["kv"]  # never $0
    assert days[-1]["total"] == 0 and days[-1]["unknown"].endswith("of unknown price")
    tile = rig.parts("/api/home")["tiles"]["data"]["spend"]
    assert tile["today"] is None and tile["pct"] is None and "stops nothing" in tile["foot"]


_OLD, _NEW = "a" * 40, "b" * 40


@pytest.mark.parametrize(
    ("asked", "progress", "paused", "shown"),
    [
        (False, None, False, None),
        (True, "running", True, ("running", [])),
        (True, "failed", True, ("failed", ["resume"])),
        (True, "done", True, ("done", ["resume"])),
    ],
    ids=["none-asked", "under-way", "stopped", "upgraded-and-paused"],
)
def test_home_says_where_an_upgrade_stands_and_offers_only_what_fits(
    rig: Rig, asked: bool, progress: str | None, paused: bool, shown: tuple[str, list[str]] | None
) -> None:
    installed = {"commit": _OLD, "owner": "o", "product": "/home/o/shop", "repo": "acme/software-factory"}
    rig.places.upgrade.mkdir(parents=True, exist_ok=True)
    store.write_json(rig.places.upgrade / "installed.json", installed)
    if asked:
        store.write_json(
            rig.places.state / "upgrade-asked.json",
            {"from": _OLD, "to": _NEW, "at": "2026-10-09T13:30:00Z", "op": "op-20261009133000-0000abcd"},
        )
    if progress:
        store.write_json(
            rig.places.upgrade / "progress.json",
            {
                "from": _OLD,
                "to": _NEW,
                "state": progress,
                "step": "Copying the factory into place",
                "problem": "A command it ran failed." if progress == "failed" else "",
                "log": "",
                "at": "2026-10-09T13:40:00Z",
            },
        )
    if paused:
        store.write_json(rig.places.state / "paused.json", {"at": "x", "sessions": [], "op": "op-x"})

    band = rig.parts("/api/home")["upgrade"]["data"]

    assert ((band["state"], band["actions"]) if band else None) == shown


def test_settings_shows_the_commit_of_the_factory_installed_here(rig: Rig) -> None:
    rig.places.upgrade.mkdir(parents=True, exist_ok=True)
    installed = {"commit": _OLD, "owner": "o", "product": "/home/o/shop", "repo": "acme/software-factory"}
    store.write_json(rig.places.upgrade / "installed.json", installed)

    assert rig.parts("/api/settings")["factory"]["data"] == {"version": _OLD[:7]}


@pytest.mark.parametrize(
    ("replay", "says"),
    [
        (None, "The replay is being made. It shows here once it is ready."),
        ({"version": 1, "error": "the transcript is gone"}, "This run has no replay: the transcript is gone."),
    ],
    ids=["not-built-yet", "failed"],
)
def test_a_replay_not_built_says_why_in_its_place(rig: Rig, replay: dict[str, Any] | None, says: str) -> None:
    built = rig.places.evidence / "t-0017" / "eeeeeeeeeeee" / "qa-ui-function" / "20261009T120000Z-abc123" / "replay"
    (built / "replay.json").unlink()
    if replay is not None:
        (built / "replay.json").write_text(json.dumps(replay))
    data = rig.parts(REPLAY_URL)["replay"]["data"]
    assert (data["title"], data["says"]) == ("Sign-in sends people back to the sign-in page", says)
    assert "rows" not in data


def test_a_checks_page_shows_each_runs_screenshots_and_a_replay_only_where_its_tester_is_known(rig: Rig) -> None:
    proof = rig.parts(f"/api/doc/{CHECKED}")["doc"]["data"]["proof"]
    assert [(r["name"], len(r["shots"]), [b["label"] for b in r["replays"]], r["note"]) for r in proof["rows"]] == [
        ("qa-regression 1", 2, ["Replay"], None),
        ("qa-regression 2", 1, [], "No replay: its tester is not known."),
    ]
    replay = rig.parts(proof["rows"][0]["replays"][0]["url"])["replay"]["data"]
    says = "The tester marked no steps in this run, so it plays as one recording."
    assert (replay["title"], replay["empty"], len(replay["frames"])) == ("Merge t-0015", says, 3)


CLEANED = "Cleaned up Oct 9: QA is kept for the newest 5 merge bundles."


def _runs(rows: list[dict[str, Any]]) -> list[tuple[str | None, list[Any], list[Any]]]:
    return [(r["note"], r["shots"], r["replays"]) for r in rows]


@pytest.mark.parametrize(
    ("of", "url", "shown", "said"),
    [
        (CHECKED, f"/api/doc/{CHECKED}", lambda p: _runs(p["doc"]["data"]["proof"]["rows"]), [(CLEANED, [], [])] * 2),
        ("t-0013", "/api/task/t-0013", lambda p: _runs(p["task"]["data"]["testers"]["rows"]),
         [("recordings cleaned up Oct 9", [], [])] * 3),
        ("t-0013", "/api/task/t-0013", lambda p: p["task"]["data"]["built_from"]["qa"],
         {"tone": None, "note": CLEANED, "shots": []}),
    ],
    ids=["check", "testers", "fix"],
)  # fmt: skip
def test_qa_the_clean_up_removed_says_when_in_place_of_its_screenshots_and_replays(
    rig: Rig, of: str, url: str, shown: Any, said: Any
) -> None:
    store.write_json(retention.cleaned_path(rig.places), {of: "2026-10-09T13:00:00Z"})

    assert shown(rig.parts(url)) == said


@pytest.mark.parametrize(
    ("level", "figure", "home", "guard"),
    [
        ("green", None, "Mains", "On mains"),
        ("green", 62.0, "62%", "Battery 62%"),
        ("amber", 25.0, "25%", "Battery 25%"),
        ("amber", None, "on battery", "On battery"),
        ("unknown", None, "unknown", "unknown"),
    ],
    ids=["mains", "charged-battery", "low-battery", "battery-unread", "supplies-unread"],
)
def test_power_reads_mains_only_when_the_reading_has_no_charge(
    rig: Rig, level: str, figure: float | None, home: str, guard: str
) -> None:
    vitals = store.read_json(rig.places.vitals)
    power = next(r for r in vitals["readings"] if r["name"] == "power")
    words = {"amber": "on battery", "unknown": "cannot read the power supplies: OSError"}
    power.update(level=level, figure=figure, value=words.get(level, "") if figure is None else "")
    store.write_json(rig.places.vitals, vitals)

    laptop = rig.parts("/api/home")["envs"]["data"]["laptop"]
    rows = rig.parts("/api/guard")["vitals"]["data"]
    assert [r["value"] for r in laptop["rows"] if r["label"] == "Power"] == [home]
    assert [r["value"] for r in rows if r["label"] == "Power"] == [guard]


SIZES = "Agents' worktrees 23.0 GiB, Docker {docker}, QA recordings 2.0 GiB, transcripts 1.0 GiB, measured 2 h ago."
MEASURED, NEXT = SIZES.format(docker="38.0 GiB"), " The next clean-up is in 4 h 0 min."
NOT_YET = "What fills the disk is not measured yet: the clean-up measures it every 6 h."
FAILED = "the Docker prune failed (exit 1)"


@pytest.mark.parametrize(
    ("docker", "change", "hint"),
    [
        (38 * 1024**3, {}, MEASURED + NEXT),
        (None, {}, SIZES.format(docker="unknown") + NEXT),
        (None, {"measured": None}, NOT_YET + NEXT),
        (38 * 1024**3, {"ended": None}, MEASURED + " A clean-up is running."),
        (38 * 1024**3, {"failed": [FAILED]}, f"The last clean-up failed: {FAILED}. {MEASURED}{NEXT}"),
    ],
    ids=["measured", "docker-unread", "not-yet", "running", "failed"],
)
def test_the_disk_shows_its_free_space_and_what_fills_it_and_how_the_clean_up_goes_one_click_behind(
    rig: Rig, docker: int | None, change: dict[str, object], hint: str
) -> None:
    record = store.read_json(rig.places.state / "gc.json")
    record["use"]["docker"] = docker
    store.write_json(rig.places.state / "gc.json", {**record, **change})

    [disk] = [g for g in rig.parts("/api/guard")["vitals"]["data"] if g["label"] == "Disk free"]
    laptop = rig.parts("/api/home")["envs"]["data"]["laptop"]

    assert (disk["value"], disk["pct"], disk["tone"], disk["hint"]) == ("112 GiB", 58, "good", hint)
    assert [r["value"] for r in laptop["rows"] if r["label"] == "Disk"] == ["42%"]
