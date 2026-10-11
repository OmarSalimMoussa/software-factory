from __future__ import annotations

import json
from pathlib import Path

import pytest
from system_fakes import settings

from runtime.shared.paths import Places
from runtime.system import layout, render

HERE = render.HERE
PLACES = Places.system()
AGENT_UID, STACK_UID = "1001", "1002"


def _values(**changes: object) -> dict[str, str]:
    return render.values(PLACES, settings(**changes), agent_uid=AGENT_UID, stack_uid=STACK_UID)


def _unit(name: str, **changes: object) -> str:
    item = next(a for a in render.UNIT_FILES if a.source.endswith(name))
    return render.artifact(item, HERE, _values(**changes))


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
    given = _values()
    for item in (*render.UNIT_FILES, render.POLKIT, render.FIREWALL):
        assert render.TOKEN.search(render.artifact(item, HERE, given)) is None, item.source


@pytest.mark.parametrize(
    ("name", "user", "uid", "runner"),
    [
        ("software-agent-run@.service", "software-agent", AGENT_UID,
         "runners/agent_run.py /var/lib/software-factory %i"),
        ("software-stack-run@.service", "software-stack", STACK_UID,
         "runners/stack_run.py /var/lib/software-factory %i"),
        ("software-agent-daemon.service", "software-agent", AGENT_UID, "runners/with_token.py"),
        ("software-services.service", "software-stack", STACK_UID, "runners/services_run.py up"),
    ],
)  # fmt: skip
def test_agent_and_stack_units_fix_their_user_home_and_slice(name: str, user: str, uid: str, runner: str) -> None:
    lines = _unit(name).splitlines()

    assert f"User={user}" in lines
    assert "Slice=software-factory.slice" in lines
    assert any(line.startswith("ExecStart=") and runner in line for line in lines)
    # Of the warden's state, only public/ is visible, read-only.
    assert "TemporaryFileSystem=/var/lib/software-factory:ro" in lines
    # %U and %h name root in a system unit, whatever User= says: the user's own id and home are written in.
    assert f"Environment=XDG_RUNTIME_DIR=/run/user/{uid}" in lines
    assert f"BindPaths=/home/{user}" in lines
    # ProtectHome=tmpfs hides /run/user too: without this bind the user's Docker socket and runtime folder vanish.
    assert f"BindPaths=/run/user/{uid}" in lines
    # Product code finds the product's toolchain first, never the owner's home it was copied from.
    assert "Environment=PATH=/opt/software-factory-toolchain/bin:/usr/local/bin:/usr/bin:/bin" in lines
    # uv finds the product's Python among the toolchain's copies, and never downloads one.
    uv = "UV_PYTHON_INSTALL_DIR=/opt/software-factory-toolchain/python UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy"
    assert f"Environment={uv}" in lines
    if user == "software-stack":
        assert f"Environment=DOCKER_HOST=unix:///run/user/{uid}/docker.sock" in lines


def test_the_warden_reads_transcripts_but_writes_only_its_state_and_the_agents_queue() -> None:
    lines = _unit("software-warden.service").splitlines()

    assert "AmbientCapabilities=CAP_DAC_READ_SEARCH" in lines
    assert "CapabilityBoundingSet=CAP_DAC_READ_SEARCH" in lines
    assert "ProtectSystem=strict" in lines
    assert "ProtectHome=read-only" in lines
    # The agent's home holds the queue, the one folder there the warden owns.
    assert f"ReadWritePaths=/var/lib/software-factory {layout.AGENT_HOME}" in lines


def test_a_check_runs_as_the_agent_in_the_slice_under_its_memory_ceiling_and_without_the_sign_in_token() -> None:
    lines = _unit("software-check-run@.service", **{"budget.test_memory_ceiling_mib": 4096}).splitlines()

    assert {"User=software-agent", "Slice=software-factory.slice", "MemoryMax=4096M", "MemorySwapMax=0",
            "OOMPolicy=continue", "TemporaryFileSystem=/var/lib/software-factory:ro"} <= set(lines)  # fmt: skip
    assert any(line.startswith("ExecStart=") and "runners/check_run.py" in line for line in lines)
    assert not any(line.startswith("LoadCredential") for line in lines)


@pytest.mark.parametrize(
    "name", ["software-agent-run@.service", "software-stack-run@.service", "software-check-run@.service"]
)
def test_a_request_units_exit_recorder_sees_the_runs_folder_and_keeps_the_output(name: str) -> None:
    lines = _unit(name).splitlines()
    runs = "/var/lib/software-factory/runs"

    # The recorder runs in the unit's mount namespace, where the state folder is hidden; truncate: would empty the
    # output when systemd opens the file again for it.
    assert f"BindPaths={runs}" in lines
    assert f"StandardOutput=append:{runs}/%i.log" in lines


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
        render.render("User=@nobody_gave_this@\n", _values())


def test_neither_user_that_runs_agent_code_reaches_the_dashboard_or_the_tailnet() -> None:
    text = render.artifact(render.FIREWALL, HERE, _values(**{"dashboard.port": 8400}))
    for user in ("software-agent", "software-stack"):
        assert f'meta skuid "{user}" tcp dport 8400 reject with tcp reset' in text
        assert f'meta skuid "{user}" ip daddr 100.64.0.0/10 meta l4proto tcp reject with tcp reset' in text
        assert f'meta skuid "{user}" ip6 daddr fd7a:115c:a1e0::/48 meta l4proto tcp reject with tcp reset' in text


def test_the_firewall_loads_after_nftables_again_on_its_timer_and_the_kernel_walls_the_slice_off_the_tailnet() -> None:
    firewall = _unit("software-firewall.service").splitlines()
    refresh = _unit("software-firewall-refresh.service").splitlines()
    timer = _unit("software-firewall-refresh.timer", **{"install.firewall_refresh_seconds": 45}).splitlines()
    stamp = "ExecStartPost=/usr/bin/touch /var/lib/software-factory/public/firewall-loaded"

    assert {"After=nftables.service", "PartOf=nftables.service", stamp} <= set(firewall)
    assert {"ExecStart=/usr/sbin/nft -f /etc/software-factory/firewall.nft", stamp} <= set(refresh)
    assert {"OnActiveSec=45s", "OnUnitActiveSec=45s", "WantedBy=timers.target"} <= set(timer)
    slice_ = _unit("software-factory.slice").splitlines()
    assert "IPAddressDeny=100.64.0.0/10 fd7a:115c:a1e0::/48" in slice_
