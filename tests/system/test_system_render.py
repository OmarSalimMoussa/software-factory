from __future__ import annotations

import json
from pathlib import Path

import pytest
from system_fakes import settings

from runtime.shared.paths import Places
from runtime.system import layout, render

HERE = render.HERE
PLACES = Places.system()
AGENT_UID = "1001"


def _values(**changes: object) -> dict[str, str]:
    return render.values(PLACES, settings(**changes), agent_uid=AGENT_UID, owner_user="deploy")


def _unit(name: str, **changes: object) -> str:
    item = next(a for a in render.UNIT_FILES if a.source.endswith(name))
    return render.artifact(item, HERE, _values(**changes))


@pytest.mark.parametrize(
    ("changes", "lines"),
    [
        ({}, ["MemoryHigh=11G", "MemoryMax=12G", "CPUWeight=50"]),
        ({"budget.slice_memory_high": "13G", "budget.slice_memory_max": "14G", "budget.slice_cpu_weight": 20},
         ["MemoryHigh=13G", "MemoryMax=14G", "CPUWeight=20"]),
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
    ("name", "user", "runner"),
    [
        ("software-agent-run@.service", "software-agent", "runners/agent_run.py /var/lib/software-factory %i"),
        ("software-stack-run@.service", "software-stack", "runners/stack_run.py /var/lib/software-factory %i"),
        ("software-agent-daemon.service", "software-agent", "runners/with_token.py"),
    ],
)
def test_agent_and_stack_units_fix_their_user_home_and_slice(name: str, user: str, runner: str) -> None:
    lines = _unit(name).splitlines()

    assert f"User={user}" in lines
    assert "Slice=software-factory.slice" in lines
    assert any(line.startswith("ExecStart=") and runner in line for line in lines)
    # Of the warden's state, only public/ is visible, read-only.
    assert "TemporaryFileSystem=/var/lib/software-factory:ro" in lines
    # %U and %h name root in a system unit, whatever User= says: the user's own id and home are written in.
    assert f"BindPaths=/home/{user}" in lines
    # ProtectHome=tmpfs hides /run/user too: without this bind the agent's runtime folder vanishes.
    assert (f"BindPaths=/run/user/{AGENT_UID}" in lines) is (user == "software-agent")
    # Product code finds the product's toolchain first, never the owner's home it was copied from.
    assert "Environment=PATH=/opt/software-factory-toolchain/bin:/usr/local/bin:/usr/bin:/bin" in lines
    # uv finds the product's Python among the toolchain's copies, and never downloads one.
    uv = "UV_PYTHON_INSTALL_DIR=/opt/software-factory-toolchain/python UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy"
    assert f"Environment={uv}" in lines


def test_a_stack_request_runs_on_the_machines_own_docker_and_only_it_reaches_that() -> None:
    stack = _unit("software-stack-run@.service").splitlines()

    assert "Environment=DOCKER_HOST=unix:///var/run/docker.sock" in stack
    assert not any("docker.sock" in line for line in stack if line.startswith("InaccessiblePaths="))
    for name in ("software-agent-run@.service", "software-agent-daemon.service", "software-check-run@.service"):
        assert "InaccessiblePaths=-/run/docker.sock" in _unit(name).splitlines(), name


def test_the_warden_reads_transcripts_but_writes_only_its_state_and_the_agents_queue() -> None:
    lines = _unit("software-warden.service").splitlines()

    assert "AmbientCapabilities=CAP_DAC_READ_SEARCH" in lines
    # git's access(2) checks ignore that capability: the agent's group lets the warden push from its checkout
    assert "SupplementaryGroups=software-slots software-agent" in lines
    assert "CapabilityBoundingSet=CAP_DAC_READ_SEARCH" in lines
    assert "ProtectSystem=strict" in lines
    assert "ProtectHome=read-only" in lines
    # The agent's home holds the queue, the one folder there the warden owns.
    assert f"ReadWritePaths=/var/lib/software-factory {layout.AGENT_HOME}" in lines


def test_a_check_runs_as_the_agent_in_the_slice_under_its_memory_ceiling_ending_with_its_warden() -> None:
    lines = _unit("software-check-run@.service", **{"budget.test_memory_ceiling_mib": 4096}).splitlines()

    assert {"User=software-agent", "Slice=software-factory.slice", "MemoryMax=4096M", "MemorySwapMax=0",
            "OOMPolicy=continue", "TemporaryFileSystem=/var/lib/software-factory:ro",
            "BindsTo=software-warden.service", "After=software-warden.service",
            "ConditionPathExists=/var/lib/software-factory/public/launch/%i.json"} <= set(lines)  # fmt: skip
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


def test_a_stack_verb_reads_its_input_from_the_wardens_own_folder() -> None:
    lines = _unit("software-stack-run@.service").splitlines()

    assert "StandardInput=file:/var/lib/software-factory/runs/%i.in" in lines


def test_managed_settings_hold_the_guard_the_rulebook_loader_and_consent_to_bypass_mode() -> None:
    body = json.loads(render.managed_settings(PLACES, settings(), HERE))

    # A background session drops the bypass mode its own settings ask for unless the managed settings consent.
    assert body["skipDangerousModePermissionPrompt"] is True
    assert set(body) == {"env", "hooks", "skipDangerousModePermissionPrompt"}
    assert body["env"] == {"MCP_TIMEOUT": str(settings().get_int("install.mcp_start_seconds") * 1000)}
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
    # Root touches the stamp in a folder of root's own, where no other user can plant a link for it to follow.
    stamp = {"RuntimeDirectory=software-firewall", "RuntimeDirectoryPreserve=yes",
             "ExecStartPost=/usr/bin/touch /run/software-firewall/loaded"}  # fmt: skip

    assert {"After=nftables.service", "PartOf=nftables.service", *stamp} <= set(firewall)
    assert {"ExecStart=/usr/sbin/nft -f /etc/software-firewall.nft", *stamp} <= set(refresh)
    assert {"OnActiveSec=45s", "OnUnitActiveSec=45s", "WantedBy=timers.target"} <= set(timer)
    slice_ = _unit("software-factory.slice").splitlines()
    assert "IPAddressDeny=100.64.0.0/10 fd7a:115c:a1e0::/48" in slice_


def test_the_agents_daemon_starts_only_with_the_firewall_loaded_and_never_restarts_with_it() -> None:
    daemon = _unit("software-agent-daemon.service").splitlines()
    # Requires= would pass on the firewall unit's restarts with nftables.service, and each ends every session.
    assert not any(line.startswith(("Requires=", "BindsTo=", "Requisite=")) for line in daemon)
    assert "ExecStartPre=+/usr/sbin/nft list table inet software_factory" in daemon


def test_the_one_unit_the_warden_starts_as_root_is_the_upgrade_to_a_full_commit() -> None:
    rules = render.artifact(render.POLKIT, HERE, _values())
    assert 'if (/^software-upgrade@[0-9a-f]{40}\\.service$/.test(unit) && verb === "start") {' in rules
    unit = _unit("software-upgrade@.service").splitlines()
    assert {"User=root", "ExecStart=/usr/bin/python3 -I /opt/software-factory/runtime/system/runners/upgrade.py %i",
            "TimeoutStartSec=90min"} <= set(unit)  # fmt: skip
    assert not any(line.startswith("Slice=") for line in unit)


def test_a_deploy_runs_as_the_owner_outside_the_slice_with_no_time_limit_that_could_cut_it_off() -> None:
    lines = _unit(layout.DEPLOY_RUN).splitlines()

    runner = "/usr/bin/python3 -I /opt/software-factory/runtime/system/runners/deploy_run.py"
    assert {"User=deploy", "TimeoutStartSec=infinity", f"ExecStart={runner} /var/lib/software-factory %i"} <= set(lines)
    # An install stops the factory's slice and restarts the warden: neither may cut a deploy off half way.
    assert not any(line.startswith(("Slice=", "BindsTo=", "PartOf=")) for line in lines)


def test_the_dashboards_stand_in_holds_its_port_never_starts_alone_and_gives_way_to_the_dashboard() -> None:
    lines = _unit(layout.UPGRADING_UNIT).splitlines()

    assert {
        "Conflicts=software-dashboard.service",
        "Before=software-dashboard.service",
        "User=software-dashboard",
    } <= set(lines)
    assert "ExecStart=/usr/bin/python3 -I /opt/software-factory/runtime/system/runners/upgrading.py 8300 15" in lines
    assert not any(line.startswith("WantedBy=") for line in lines)
