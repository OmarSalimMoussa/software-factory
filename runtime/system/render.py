"""Turns the templates in this folder into the files the installer writes, from the paths and the settings.

A template marks each value as ``@name@``, a form that no unit, polkit rule, nftables file or JSON uses for itself.
A template naming a value that is not given fails loudly, so no file is ever written half filled.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from runtime.shared import error_reports
from runtime.shared.paths import AGENT_USER, DASHBOARD_USER, MANAGED_SETTINGS, SLICE, WARDEN_USER, Places
from runtime.shared.settings import Settings
from runtime.system import layout
from runtime.system.errors import instance

HERE = Path(__file__).resolve().parent
TOKEN = re.compile(r"@([a-z_]+)@")
_ORG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")


class RenderError(Exception):
    """A template names a value nobody gave it, or renders to a file that does not parse."""


@dataclass(frozen=True, slots=True)
class Artifact:
    source: str  # relative to this folder, so the installer reads it from the sealed commit
    dest: Path
    mode: int


# What the units step writes, in this order. The managed settings and the AppArmor profile have their own steps.
UNIT_FILES = (
    Artifact("units/software-factory.slice", layout.UNITS_DIR / SLICE, 0o644),
    Artifact(f"units/{layout.FIREWALL_UNIT}", layout.UNITS_DIR / layout.FIREWALL_UNIT, 0o644),
    Artifact(f"units/{layout.FIREWALL_REFRESH}", layout.UNITS_DIR / layout.FIREWALL_REFRESH, 0o644),
    Artifact(f"units/{layout.FIREWALL_TIMER}", layout.UNITS_DIR / layout.FIREWALL_TIMER, 0o644),
    Artifact(f"units/{layout.WARDEN_UNIT}", layout.UNITS_DIR / layout.WARDEN_UNIT, 0o644),
    Artifact(f"units/{layout.DASHBOARD_UNIT}", layout.UNITS_DIR / layout.DASHBOARD_UNIT, 0o644),
    Artifact(f"units/{layout.UPGRADING_UNIT}", layout.UNITS_DIR / layout.UPGRADING_UNIT, 0o644),
    Artifact(f"units/{layout.DAEMON_UNIT}", layout.UNITS_DIR / layout.DAEMON_UNIT, 0o644),
    Artifact(f"units/{layout.AGENT_RUN}", layout.UNITS_DIR / layout.AGENT_RUN, 0o644),
    Artifact(f"units/{layout.STACK_RUN}", layout.UNITS_DIR / layout.STACK_RUN, 0o644),
    Artifact(f"units/{layout.CHECK_RUN}", layout.UNITS_DIR / layout.CHECK_RUN, 0o644),
    Artifact(f"units/{layout.UPGRADE_RUN}", layout.UNITS_DIR / layout.UPGRADE_RUN, 0o644),
    Artifact(f"units/{layout.SERVICES_UNIT}", layout.UNITS_DIR / layout.SERVICES_UNIT, 0o644),
    Artifact(f"units/{layout.ERRORS_UNIT}", layout.UNITS_DIR / layout.ERRORS_UNIT, 0o644),
    Artifact("units/system-sleep.sh", layout.SLEEP_HOOK, 0o755),
)
POLKIT = Artifact("polkit/50-software-factory.rules", layout.POLKIT_RULE, 0o644)


def stack_wall(stack_uid: str) -> Artifact:
    """The drop-in that walls the stack user's own systemd off the tailnet; its place names the user's id."""
    return Artifact("units/stack-user-tailnet.conf", layout.stack_wall(stack_uid), 0o644)


FIREWALL = Artifact("nftables/software-factory.nft", layout.NFT_FILE, 0o644)
APPARMOR = Artifact("apparmor/software-bwrap", layout.APPARMOR_PROFILE, 0o644)
MANAGED = Artifact("managed-settings.json", MANAGED_SETTINGS, 0o644)


def render(text: str, values: Mapping[str, str]) -> str:
    missing = sorted({name for name in TOKEN.findall(text) if name not in values})
    if missing:
        raise RenderError(f"the template needs values for {', '.join(missing)}")
    return TOKEN.sub(lambda found: values[found.group(1)], text)


def values(places: Places, settings: Settings, *, agent_uid: str, stack_uid: str, errors_uid: str) -> dict[str, str]:
    """Every value a unit, rule or firewall template may name. The users' ids are the machine's, so a unit never
    uses %U or %h: in a system unit those name root, whatever its User= says."""
    return {
        "python": layout.PYTHON,
        "install": str(places.install),
        "state": str(places.state),
        "public": str(layout.public(places.state)),
        "runs": str(layout.runs(places.state)),
        "secrets": str(layout.secrets(places.state)),
        "factory_dsn": str(error_reports.dsn_path(places)),
        "slots": str(places.slots),
        "run_name": places.run.name,
        "claude": str(layout.CLAUDE_BIN),
        "claude_token": layout.CLAUDE_TOKEN,
        "bwrap": str(layout.BWRAP),
        "warden_user": WARDEN_USER,
        "agent_user": AGENT_USER,
        "stack_user": layout.STACK_USER,
        "dashboard_user": DASHBOARD_USER,
        "slots_group": layout.SLOTS_GROUP,
        "agent_home": str(layout.AGENT_HOME),
        "stack_home": str(layout.STACK_HOME),
        "agent_uid": agent_uid,
        "stack_uid": stack_uid,
        "stack_socket": layout.rootless_socket(stack_uid),
        "errors_user": layout.ERRORS_USER,
        "errors_home": str(layout.ERRORS_HOME),
        "errors_uid": errors_uid,
        "errors_socket": layout.rootless_socket(errors_uid),
        "errors_port": str(instance.PORT),
        "path": layout.UNIT_PATH,
        "uv_env": " ".join(f"{key}={value}" for key, value in layout.UV_ENV.items()),
        "slice": SLICE,
        "warden_unit": layout.WARDEN_UNIT,
        "dashboard_unit": layout.DASHBOARD_UNIT,
        "daemon_unit": layout.DAEMON_UNIT,
        "firewall_unit": layout.FIREWALL_UNIT,
        "nft_file": str(layout.NFT_FILE),
        "nft_table": layout.NFT_TABLE,
        "firewall_run_name": places.firewall.name,
        "firewall_stamp": str(places.firewall_stamp),
        "firewall_refresh": f"{settings.get_int('install.firewall_refresh_seconds')}s",
        "memory_high": settings.get_str("budget.slice_memory_high"),
        "memory_max": settings.get_str("budget.slice_memory_max"),
        "cpu_weight": str(settings.get_int("budget.slice_cpu_weight")),
        "test_memory_max": f"{settings.get_int('budget.test_memory_ceiling_mib')}M",
        "restart_seconds": str(settings.get_int("watch.tick_seconds")),
        "upgrade_minutes": f"{settings.get_int('install.upgrade_minutes')}min",
        "dashboard_port": str(settings.get_int("dashboard.port")),
        "dashboard_refresh": str(settings.get_int("dashboard.refresh_seconds")),
    }


def artifact(item: Artifact, source: Path, given: Mapping[str, str]) -> str:
    """One template from the folder ``source`` (this folder in the sealed commit), rendered."""
    return render((source / item.source).read_text(encoding="utf-8"), given)


def product_org(settings: Settings) -> str:
    """The GitHub owner of the product repo: its sessions get the rulebook."""
    org = settings.get_str("repo.github").partition("/")[0]
    if not _ORG.match(org):
        raise RenderError(f"repo.github must read owner/name, not {settings.get_str('repo.github')!r}")
    return org


def managed_settings(places: Places, settings: Settings, source: Path) -> str:
    """Claude Code's managed settings: the guard on every tool call, every start and stop of a session or a
    subagent, and the rulebook loader at every start."""
    guard = f"{layout.PYTHON} -I {places.install}/runtime/guard/hook.py"
    loader = (
        f"{layout.PYTHON} -I {places.install}/plugin/hooks/rulebook_loader.py"
        f" --org {product_org(settings)} --slice {SLICE}"
    )
    given = {
        "guard": json.dumps(guard)[1:-1],
        "loader": json.dumps(loader)[1:-1],
        "hook_timeout": str(settings.get_int("install.hook_timeout_seconds")),
    }
    text = artifact(MANAGED, source, given)
    try:
        json.loads(text)
    except ValueError as exc:
        raise RenderError(f"the managed settings do not parse: {exc}") from exc
    return text


# How a service is walled in, which a drill copies onto its own command; and what it runs, as whom, which it leaves.
_WALLS = frozenset({"Slice", "Environment", "UMask", "TemporaryFileSystem", "BindReadOnlyPaths", "BindPaths",
                    "ProtectHome", "InaccessiblePaths", "NoNewPrivileges", "ProtectSystem", "ProtectKernelModules",
                    "RestrictSUIDSGID", "RestrictRealtime", "LockPersonality"})  # fmt: skip
_RUNS = frozenset({"Type", "ExecStart", "ExecStartPre", "Restart", "RestartSec", "KillMode", "LoadCredential", "User",
                   "Group"})  # fmt: skip


def walls(unit: str) -> tuple[str, ...]:
    """How a rendered unit walls in what it runs: its [Service] settings in _WALLS; one in neither list raises."""
    section, found = "", []
    for line in unit.splitlines():
        text = line.strip()
        key, equals, _ = text.partition("=")
        if text.startswith("["):
            section = text
        elif section != "[Service]" or not equals or text.startswith("#") or key in _RUNS:
            continue
        elif key not in _WALLS:
            raise RenderError(f"{key}= is neither a wall a drill copies nor what the unit runs: sort it in render.py")
        else:
            found.append(text)
    return tuple(found)
