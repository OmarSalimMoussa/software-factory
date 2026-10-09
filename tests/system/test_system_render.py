from __future__ import annotations

import json
from pathlib import Path

import pytest
from system_fakes import settings

from runtime.shared.paths import Places
from runtime.system import render

HERE = render.HERE
PLACES = Places.system()


def _unit(name: str, **changes: object) -> str:
    item = next(a for a in render.UNIT_FILES if a.source.endswith(name))
    return render.artifact(item, HERE, render.values(PLACES, settings(**changes)))


@pytest.mark.parametrize(
    ("changes", "lines"),
    [
        ({}, ["MemoryHigh=11G", "MemoryMax=12G", "CPUWeight=50"]),
        ({"budget.slice_memory_high": "6G", "budget.slice_memory_max": "7G", "budget.slice_cpu_weight": 20},
         ["MemoryHigh=6G", "MemoryMax=7G", "CPUWeight=20"]),
    ],
)  # fmt: skip
def test_the_slice_takes_its_budget_from_the_settings(changes: dict[str, object], lines: list[str]) -> None:
    text = _unit("software-factory.slice", **changes)

    assert [
        line for line in text.splitlines() if line.split("=")[0] in ("MemoryHigh", "MemoryMax", "CPUWeight")
    ] == lines


def test_every_unit_renders_whole() -> None:
    given = render.values(PLACES, settings())
    for item in (*render.UNIT_FILES, render.POLKIT, render.FIREWALL):
        assert render.TOKEN.search(render.artifact(item, HERE, given)) is None, item.source


@pytest.mark.parametrize(
    ("name", "user", "runner"),
    [
        ("software-agent-run@.service", "software-agent", "runners/agent_run.py /var/lib/software-factory %i"),
        ("software-stack-run@.service", "software-stack", "runners/stack_run.py /var/lib/software-factory %i"),
        ("software-agent-daemon.service", "software-agent", "runners/with_token.py"),
    ],
)
def test_agent_and_stack_units_fix_their_user_and_slice(name: str, user: str, runner: str) -> None:
    lines = _unit(name).splitlines()

    assert f"User={user}" in lines
    assert "Slice=software-factory.slice" in lines
    assert any(line.startswith("ExecStart=") and runner in line for line in lines)
    # Of the warden's state, only public/ is visible, read-only.
    assert "TemporaryFileSystem=/var/lib/software-factory:ro" in lines


def test_the_warden_reads_transcripts_but_writes_nothing_outside_its_state() -> None:
    lines = _unit("software-warden.service").splitlines()

    assert "AmbientCapabilities=CAP_DAC_READ_SEARCH" in lines
    assert "CapabilityBoundingSet=CAP_DAC_READ_SEARCH" in lines
    assert "ProtectSystem=strict" in lines
    assert "ReadWritePaths=/var/lib/software-factory" in lines


def test_managed_settings_hold_only_the_guard_and_the_rulebook_loader() -> None:
    body = json.loads(render.managed_settings(PLACES, settings(), HERE))

    assert set(body) == {"hooks"}
    assert set(body["hooks"]) == {"PreToolUse", "SessionStart", "SubagentStart", "Stop", "SubagentStop"}
    commands = {hook["command"] for groups in body["hooks"].values() for group in groups for hook in group["hooks"]}
    assert commands == {
        "/usr/bin/python3 -I /opt/software-factory/runtime/guard/hook.py",
        "/usr/bin/python3 -I /opt/software-factory/plugin/hooks/rulebook_loader.py --org acme "
        "--slice software-factory.slice",
    }
    assert body["hooks"]["PreToolUse"][0]["matcher"] == "*"


def test_a_template_naming_an_unknown_value_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(render.RenderError, match="nobody_gave_this"):
        render.render("User=@nobody_gave_this@\n", render.values(PLACES, settings()))
