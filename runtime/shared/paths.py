"""Where everything lives on the machine.

The installed factory is read-only under /opt; the warden's state under /var/lib; its sockets under /run. A test
or a rehearsal builds ``Places`` on a temporary root instead, so nothing here is ever read from the environment.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

INSTALL = Path("/opt/software-factory")
STATE = Path("/var/lib/software-factory")
RUN = Path("/run/software-factory")
MANAGED_SETTINGS = Path("/etc/claude-code/managed-settings.json")
WARDEN_USER = "software-warden"
AGENT_USER = "software-agent"
SLICE = "software-factory.slice"


@dataclass(frozen=True, slots=True)
class Places:
    install: Path
    state: Path
    run: Path

    @classmethod
    def system(cls) -> Places:
        return cls(INSTALL, STATE, RUN)

    @classmethod
    def under(cls, root: Path) -> Places:
        """A self-contained tree for tests and the rehearsal."""
        return cls(root / "install", root / "state", root / "run")

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
    def decisions(self) -> Path:
        return self.state / "decisions.jsonl"

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
    def probes(self) -> Path:
        return self.state / "probes.jsonl"

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

    @property
    def ready(self) -> Path:
        return self.state / "ready"

    @property
    def slots(self) -> Path:
        return self.state / "slots"

    def slot(self, n: int) -> Path:
        return self.slots / str(n)

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
        return self.state / "plugin"

    # sockets
    @property
    def socket(self) -> Path:
        return self.run / "warden.sock"

    @property
    def refusal_socket(self) -> Path:
        return self.run / "refusals.sock"


def queue_dir(repo: Path) -> Path:
    return repo / "queue"
