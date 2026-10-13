"""Where everything lives on the machine.

The installed factory is read-only under /opt; the warden's state under /var/lib; its sockets under /run. A test
or a rehearsal builds ``Places`` on a temporary root instead, so nothing here is ever read from the environment.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

INSTALL = Path("/opt/software-factory")
STATE = Path("/var/lib/software-factory")
RUN = Path("/run/software-factory")
# Root's alone: the firewall units' runtime folder, where each load of the table leaves its time.
FIREWALL = Path("/run/software-firewall")
# Root's alone, readable by all: what is installed, as root knows it, and the progress of an upgrade under way.
UPGRADE = Path("/var/lib/software-factory-upgrade")
UPGRADE_UNIT = "software-upgrade@{}.service"  # the owner's upgrade to a release commit, the warden's one root unit
MANAGED_SETTINGS = Path("/etc/claude-code/managed-settings.json")
# On every user's PATH, so a session types the bare word: the sandbox exempts a call by its text, `factory …` alone.
FACTORY_BIN = Path("/usr/local/bin/factory")
# The pinned check tools' virtual environment: `factory check` runs under its Python, with ruff and mypy beside it.
CHECK_TOOLS = Path("/opt/software-factory-tools")
# Where a session writes a body it hands the factory command, inside its worktree; git ignores it (the installer adds
# it to the checkout's info/exclude), so it never counts as uncommitted work.
SCRATCH = ".factory"
# contract: the product's prod workflow, the factory's CI template copied under this name; the warden starts it.
PROMOTE_WORKFLOW = "deploy-prod.yml"
CI_WORKFLOW = "ci.yml"  # the product's CI, which checks every push and deploys the default branch to staging
# The job in it that moves prod; the template names it so, and the warden judges a push by it alone.
PROMOTE_JOB = "deploy"
WARDEN_USER = "software-warden"
AGENT_USER = "software-agent"
DASHBOARD_USER = "software-dashboard"  # contract: added by 1B-3, the one other user owner.sock answers
SLICE = "software-factory.slice"


def stored_signin(home: Path) -> Path:
    """Claude Code's stored sign-in for the user whose home is ``home``: what its daemon hands its sessions."""
    return home / ".claude" / ".credentials.json"


# contract: added by FB5. The stack user's own folder of slots: each slot's env file and provisioning state, which
# the stack user's runs write and the warden reads only to redact.
STACK_SLOTS = Path("/home/software-stack/slots")
# contract: added by FB5. The laptop's shared services: their compose file and services.env, root's, read by the stack
# user. Not in the state folder, whose 0700 the stack user's Docker could not pass to bind a file from it.
SERVICES = Path("/var/lib/software-factory-services")
# The factory's own GlitchTip, apart from the shared services: its compose file and errors.env.
ERRORS = Path("/var/lib/software-factory-errors")


@dataclass(frozen=True, slots=True)
class Places:
    install: Path
    state: Path
    run: Path
    stack_slots: Path = STACK_SLOTS
    services: Path = SERVICES
    errors: Path = ERRORS
    firewall: Path = FIREWALL
    upgrade: Path = UPGRADE

    @classmethod
    def system(cls) -> Places:
        return cls(INSTALL, STATE, RUN)

    @classmethod
    def under(cls, root: Path) -> Places:
        """A self-contained tree for tests and the rehearsal."""
        return cls(
            root / "install",
            root / "state",
            root / "run",
            root / "stack-slots",
            root / "services",
            root / "errors",
            root / "firewall",
            root / "upgrade",
        )

    @classmethod
    def for_runner(cls, install: Path, state: Path) -> Places:
        """A unit runner's places, from the state folder its unit names: the machine's, or those of a tree laid out
        as ``under`` lays one out (the rehearsal's)."""
        if state == STATE:
            return cls(install, STATE, RUN)
        return replace(cls.under(state.parent), install=install)

    # the install
    @property
    def defaults(self) -> Path:
        return self.install / "settings.toml"

    @property
    def plugin(self) -> Path:
        return self.install / "plugin"

    # the warden's state
    @property
    def overrides(self) -> Path:
        return self.state / "settings" / "overrides.toml"

    @property
    def audit(self) -> Path:
        return self.state / "settings" / "audit.jsonl"

    @property
    def registry(self) -> Path:
        return self.state / "registry.json"

    @property
    def history(self) -> Path:
        return self.state / "history.jsonl"

    @property
    def drill_permits(self) -> Path:
        return self.state / "drill-permits.json"

    @property
    def approvals(self) -> Path:
        return self.state / "approvals"

    @property
    def proposals(self) -> Path:
        return self.state / "proposals.json"

    @property
    def cards(self) -> Path:
        return self.state / "cards.json"

    @property
    def pictures(self) -> Path:
        return self.state / "pictures"

    @property
    def firewall_stamp(self) -> Path:
        """Touched by root each time the firewall table loads; the firewall vital reads its time. In root's own
        folder: root's touch follows a link, which another user could plant in a folder it writes."""
        return self.firewall / "loaded"

    @property
    def github_known_hosts(self) -> Path:
        """github.com's SSH host keys, as GitHub publishes them, which the warden's git trusts and nothing else."""
        return self.state / ".ssh" / "known_hosts"

    @property
    def public_turn(self) -> Path:
        """The branch holding the merge turn, for the merge hook, which runs as the agent and reads public/."""
        return self.state / "public" / "turn.json"

    @property
    def check_passes(self) -> Path:
        """Every tree a whole `factory check` passed, in the warden's state."""
        return self.state / "check-passes.json"

    @property
    def public_check_passes(self) -> Path:
        """The passed trees alone, for the merge hook, which runs as the agent and reads public/."""
        return self.state / "public" / "check-passes.json"

    @property
    def probes(self) -> Path:
        return self.state / "probes.jsonl"

    @property
    def deploys(self) -> Path:  # contract: each new build staging and prod answered with, appended by the loop
        return self.state / "deploys.jsonl"

    @property
    def inbox(self) -> Path:
        return self.state / "inbox.jsonl"

    @property
    def cost(self) -> Path:
        return self.state / "cost.jsonl"

    @property
    def cost_cursor(self) -> Path:
        return self.state / "cost-cursor.json"

    @property
    def plan(self) -> Path:
        return self.state / "plan.json"

    @property
    def switches(self) -> Path:
        return self.state / "switches.json"

    @property
    def refusals(self) -> Path:
        return self.state / "refusals.jsonl"

    @property
    def reflections(self) -> Path:
        return self.state / "SELF_REFLECT.md"

    def ready(self, session_id: str) -> Path:
        """An implementer's QA-ready records, in public/ where the guard reads them."""
        return self.state / "public" / "ready" / f"{session_id}.json"

    @property
    def slots(self) -> Path:
        return self.state / "slots"

    def slot(self, n: int) -> Path:
        return self.slots / str(n)

    # contract: added by FB5. The slot's env file (FACTORY_ENV_FILE), and what the product's scripts/dev writes beside
    # it, such as seeds and temporary files: the factory reads and removes the env file alone.
    def slot_env_dir(self, n: int) -> Path:
        return self.stack_slots / str(n) / "env"

    def slot_state_dir(self, n: int) -> Path:  # contract: added by FB5; the slot's provisioning state
        return self.stack_slots / str(n) / "state"

    @property
    def evidence(self) -> Path:
        return self.state / "evidence"

    @property
    def totp_secret(self) -> Path:
        return self.state / "totp.key"

    @property
    def code_lock(self) -> Path:
        return self.state / "code-lock.json"

    @property
    def vitals(self) -> Path:
        return self.state / "vitals.json"

    @property
    def install_hash(self) -> Path:
        return self.state / "install-hash"

    @property
    def rendered_plugin(self) -> Path:
        # contract: changed by 1B-3 to public/, where agent sessions can read it; procedures/ and rules/ sit beside it
        return self.state / "public" / "plugin"

    @property
    def rendered_agents(self) -> Path:
        return self.state / "public" / "agents"

    # sockets
    @property
    def socket(self) -> Path:
        return self.run / "warden.sock"

    @property
    def refusal_socket(self) -> Path:
        return self.run / "refusals.sock"

    @property
    def owner_socket(self) -> Path:  # contract: added by 1B-3, the dashboard's and the owner's door
        return self.run / "owner.sock"

    @property
    def owner(self) -> Path:  # contract: added by 1B-3, {"user": "<the owner's login>"}, written by the installer
        return self.state / "owner.json"


def queue_dir(repo: Path) -> Path:
    return repo / "queue"
