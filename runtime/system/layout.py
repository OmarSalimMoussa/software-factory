"""Where the installer puts things on the machine, beside what runtime/shared/paths.py already names.

Every path a unit, a rule or another module must agree on is named once here. The installer renders the units and
rules from these values, so no template spells a path of its own.
"""

from __future__ import annotations

from pathlib import Path

from runtime.shared import paths
from runtime.shared.paths import AGENT_USER, DASHBOARD_USER, WARDEN_USER
from standard.server import edge
from standard.services import stack

STACK_USER = "software-stack"
# The factory's own GlitchTip runs on this user's rootless Docker alone: no user that runs agent code reaches it.
ERRORS_USER = "software-errors"
# The group that reads the warden's slot folders (each verb's FACTORY_SLOT_DIR): the stack user's runs; units add it.
SLOTS_GROUP = "software-slots"
USERS = (WARDEN_USER, AGENT_USER, STACK_USER, DASHBOARD_USER, ERRORS_USER)
# A factory user in any of these could reach root, Docker, the journal or a disk: the installer refuses.
PRIVILEGED_GROUPS = frozenset(
    {"root", "sudo", "admin", "wheel", "adm", "docker", "systemd-journal", "lxd", "libvirt", "disk", "shadow", "kvm"}
)
AGENT_HOME = Path("/home") / AGENT_USER
STACK_HOME = Path("/home") / STACK_USER
ERRORS_HOME = Path("/home") / ERRORS_USER
# Each user whose own rootless Docker runs a stack, with its home.
DOCKER_USERS = {STACK_USER: STACK_HOME, ERRORS_USER: ERRORS_HOME}

PYTHON = "/usr/bin/python3"
# The factory command, on every user's PATH.
FACTORY_BIN = paths.FACTORY_BIN
# The owner's own change to a path only a person changes, which root commits (runtime/system/owner_commit.py).
OWN_BIN = Path("/usr/local/sbin/factory-own")
# The agents' own Claude Code: root-owned, so no session can replace the program that runs its guard.
CLAUDE_BIN = Path("/usr/local/libexec/software-factory/claude")
TOOLS = paths.CHECK_TOOLS
# The product's toolchain (install.toolchain), copied from the owner's PATH: root-owned, one folder per program, and
# a link to every program it holds in bin/.
TOOLCHAIN = Path("/opt/software-factory-toolchain")
TOOLCHAIN_BIN = TOOLCHAIN / "bin"
# The owner's uv-managed Pythons that the product asks for, one folder each, named as uv names them.
TOOLCHAIN_PYTHON = TOOLCHAIN / "python"
# What every unit that runs product code finds on its PATH: the toolchain first, then the system's own folders.
SYSTEM_PATH = ("/usr/local/bin", "/usr/bin", "/bin")
UNIT_PATH = ":".join((str(TOOLCHAIN_BIN), *SYSTEM_PATH))
# What those units also set: uv finds the product's Python among the toolchain's copies and never downloads one.
# Copy, never hard-link, from the cache every session shares: a session editing a file in its own venv must not edit
# the cache another session installs from.
UV_ENV = {"UV_PYTHON_INSTALL_DIR": str(TOOLCHAIN_PYTHON), "UV_PYTHON_DOWNLOADS": "never", "UV_LINK_MODE": "copy"}
# Where each install runs from: a sealed, root-owned and read-only copy of one commit, in a folder of its own named
# <sha>-<random>, removed when it ends.
STAGE_ROOT = Path("/var/lib/software-factory-stage")
# Where each install and uninstall keeps its log: every command, its output and how long it took. Root writes it; the
# owner's group reads it.
LOGS = Path("/var/log/software-factory")
# The one PATH the installer runs with, and every command it runs as root.
ROOT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

UNITS_DIR = Path("/etc/systemd/system")
SLICE_CONTROL = Path("/etc/systemd/system.control")  # where `systemctl set-property` keeps its drop-ins
SLEEP_HOOK = Path("/usr/lib/systemd/system-sleep/software-factory")
POLKIT_RULE = Path("/etc/polkit-1/rules.d/50-software-factory.rules")
# A file of the factory's own: /etc/software-factory holds the server set-up's product.toml and heartbeat-ping.
NFT_FILE = Path("/etc/software-firewall.nft")
NFT_TABLE = "software_factory"
# The dashboard's site on the box's edge, which the server set-up runs; a deploy writes only its workspaces' sites.
EDGE_SITE = edge.SITES / "factory.caddy"
APPARMOR_DIR = Path("/etc/apparmor.d")
APPARMOR_PROFILE = APPARMOR_DIR / "software-bwrap"
BWRAP = Path("/usr/bin/bwrap")

WARDEN_UNIT = "software-warden.service"
DASHBOARD_UNIT = "software-dashboard.service"
# The dashboard's stand-in while an upgrade runs; starting the dashboard stops it.
UPGRADING_UNIT = "software-upgrading.service"
DAEMON_UNIT = "software-agent-daemon.service"
FIREWALL_UNIT = "software-firewall.service"
FIREWALL_REFRESH = "software-firewall-refresh.service"
FIREWALL_TIMER = "software-firewall-refresh.timer"
AGENT_RUN = "software-agent-run@.service"
STACK_RUN = "software-stack-run@.service"
SLOT_KEYS_UNIT = "software-slot-keys.service"
CHECK_RUN = "software-check-run@.service"
UPGRADE_RUN = paths.UPGRADE_UNIT.format("")
SERVICES = (WARDEN_UNIT, DASHBOARD_UNIT, DAEMON_UNIT)
# The laptop's shared services (the stack of standard/services/), run by the stack user; a reinstall leaves them up.
SERVICES_UNIT = "software-services.service"
# The factory's own GlitchTip, run by its own user; a reinstall leaves it up.
ERRORS_UNIT = "software-errors.service"

PACKAGES = ("bubblewrap", "uidmap", "nftables", "apparmor-utils")
ROOTLESS_SETUP = "dockerd-rootless-setuptool.sh"
ROOTLESS_EXTRAS = "docker-ce-rootless-extras"  # the package of Docker's own that brings it
CLAUDE_TOKEN = "claude-token"  # the credential name the agent units load the sign-in token under


# The machine's own services.env; code that may run in a test tree uses Places.services instead.
SERVICES_ENV = paths.SERVICES / stack.ENV_FILE
# Where the shared services answer on this laptop's loopback.
GLITCHTIP_URL = f"http://127.0.0.1:{stack.PORTS['glitchtip']}"
LANGFUSE_URL = f"http://127.0.0.1:{stack.PORTS['langfuse']}"
GLITCHTIP_HEALTH = f"{GLITCHTIP_URL}/_health/"
LANGFUSE_HEALTH = f"{LANGFUSE_URL}/api/public/health"
# In the secrets folder, the warden's alone: the laptop GlitchTip's read token, the laptop Langfuse project's keys, and
# the server's keys the server set-up saved.
LAPTOP_GLITCHTIP = "laptop-glitchtip.json"
LAPTOP_LANGFUSE = "laptop-langfuse.json"
SERVER_KEYS = "server.json"
# The owner's copy of the server's keys, written by `python3 -m standard.server.setup`.
OWNER_SERVER_KEYS = Path(".config") / "software-factory" / SERVER_KEYS


def rootless_socket(uid: int | str) -> str:
    """A user's own Docker socket, which its rootless daemon opens in its runtime folder."""
    return f"/run/user/{uid}/docker.sock"


def secrets(state: Path) -> Path:
    return state / "secrets"


def runs(state: Path) -> Path:
    """Where systemd, as root, writes each template unit's output and exit status for the warden."""
    return state / "runs"


def public(state: Path) -> Path:
    return state / "public"


def install_record(state: Path) -> Path:
    return state / "install.json"


def stack_wall(stack_uid: str) -> Path:
    """The drop-in that walls the stack user's systemd off the tailnet, in a folder that is all the factory's, as
    the user is."""
    return UNITS_DIR / f"user@{stack_uid}.service.d" / "software-tailnet.conf"
