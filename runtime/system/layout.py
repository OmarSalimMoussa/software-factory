"""Where the installer puts things on the machine, beside what runtime/shared/paths.py already names.

Every path a unit, a rule or another module must agree on is named once here. The installer renders the units and
rules from these values, so no template spells a path of its own.
"""

from __future__ import annotations

from pathlib import Path

from runtime.shared import paths
from runtime.shared.paths import AGENT_USER, DASHBOARD_USER, WARDEN_USER

STACK_USER = "software-stack"
# The group that reads the slots' env files: the warden writes them, the stack user's runs read them; units add it.
SLOTS_GROUP = "software-slots"
USERS = (WARDEN_USER, AGENT_USER, STACK_USER, DASHBOARD_USER)
# A factory user in any of these could reach root, Docker, the journal or a disk: the installer refuses.
PRIVILEGED_GROUPS = frozenset(
    {"root", "sudo", "admin", "wheel", "adm", "docker", "systemd-journal", "lxd", "libvirt", "disk", "shadow", "kvm"}
)
AGENT_HOME = Path("/home") / AGENT_USER
STACK_HOME = Path("/home") / STACK_USER

PYTHON = "/usr/bin/python3"
# The factory command. Sessions type the bare word alone, which is the one text the sandbox lets out.
FACTORY_BIN = paths.FACTORY_BIN
# The agents' own Claude Code: root-owned, so no session can replace the program that runs its guard.
CLAUDE_BIN = Path("/usr/local/libexec/software-factory/claude")
TOOLS = paths.CHECK_TOOLS
STAGE_PREFIX = "software-factory-stage-"

UNITS_DIR = Path("/etc/systemd/system")
SLICE_CONTROL = Path("/etc/systemd/system.control")  # where `systemctl set-property` keeps its drop-ins
SLEEP_HOOK = Path("/usr/lib/systemd/system-sleep/software-factory")
POLKIT_RULE = Path("/etc/polkit-1/rules.d/50-software-factory.rules")
CONFIG = Path("/etc/software-factory")
NFT_FILE = CONFIG / "firewall.nft"
NFT_TABLE = "software_factory"
APPARMOR_DIR = Path("/etc/apparmor.d")
APPARMOR_PROFILE = APPARMOR_DIR / "software-bwrap"
BWRAP = Path("/usr/bin/bwrap")
DRILLS = Path("/run/software-drills")

WARDEN_UNIT = "software-warden.service"
DASHBOARD_UNIT = "software-dashboard.service"
DAEMON_UNIT = "software-agent-daemon.service"
FIREWALL_UNIT = "software-firewall.service"
AGENT_RUN = "software-agent-run@.service"
STACK_RUN = "software-stack-run@.service"
SERVICES = (WARDEN_UNIT, DASHBOARD_UNIT, DAEMON_UNIT)

PACKAGES = ("bubblewrap", "socat", "uidmap", "nftables", "apparmor-utils")
ROOTLESS_SETUP = "dockerd-rootless-setuptool.sh"
CLAUDE_TOKEN = "claude-token"  # the credential name the agent units load the sign-in token under


def secrets(state: Path) -> Path:
    return state / "secrets"


def runs(state: Path) -> Path:
    """Where systemd, as root, writes each template unit's output and exit status for the warden."""
    return state / "runs"


def public(state: Path) -> Path:
    return state / "public"


def install_record(state: Path) -> Path:
    return state / "install.json"
