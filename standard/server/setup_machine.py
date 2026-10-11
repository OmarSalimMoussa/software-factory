"""The set-up kit's hold on the server: one SSH runner for every command, files and folders made to match, and the
machine checks it never changes on an existing server.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from standard.config import Config
from standard.services.provision import Run

# The factory checkout the set-up runs from: its pyproject.toml says which Python the factory needs.
FACTORY = Path(__file__).resolve().parents[2]

LOCKDOWN = "/etc/ssh/sshd_config.d/00-factory-lockdown.conf"
# sshd keeps the first value it reads for a setting, and reads the drop-ins in name order before its main file.
LOCKDOWN_TEXT = """# Written by the factory's server set-up (--lockdown): keys only, no root.
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
"""
OPEN_PORTS = ("22", "80", "443")
SSH_SECONDS = 1800

UFW = ["sudo", "-n", "ufw", "status"]
SSHD = ["sudo", "-n", "sshd", "-T"]
TAILSCALE = ["sudo", "-n", "sh", "-c", "command -v tailscale >/dev/null || exit 3; tailscale status --json"]
SWAP = ["swapon", "--show", "--noheadings"]
# The interpreter every factory command on the server starts with (its wrappers' #! line).
PYTHON = ["/usr/bin/python3", "-c", "import sys; print('%d.%d' % sys.version_info[:2])"]
DOCKER = ["sudo", "-n", "sh", "-c", 'docker version --format "{{.Server.Version}}" && docker compose version --short']

_APT = ["sudo", "-n", "env", "DEBIAN_FRONTEND=noninteractive", "apt-get"]
_SWAPFILE = (
    "set -e; fallocate -l 4G /swapfile; chmod 600 /swapfile; mkswap /swapfile; swapon /swapfile; "
    "grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab"
)
_DOCKER_REPO = (
    "set -e; export DEBIAN_FRONTEND=noninteractive; apt-get update; apt-get install -y ca-certificates curl; "
    "install -m 0755 -d /etc/apt/keyrings; "
    "curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc; "
    "chmod a+r /etc/apt/keyrings/docker.asc; . /etc/os-release; "
    'echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] '
    'https://download.docker.com/linux/ubuntu $VERSION_CODENAME stable" > /etc/apt/sources.list.d/docker.list; '
    "apt-get update; "
    "apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin"
)
_LOCK = (
    'set -e; umask 022; t=$(mktemp); cat > "$t"; install -m 644 -o root -g root "$t" "$1"; rm -f "$t"; '
    'if ! sshd -t; then rm -f "$1"; echo "sshd refused the settings; they were removed" >&2; exit 1; fi; '
    "systemctl restart ssh.socket 2>/dev/null || systemctl restart ssh"
)

# Exit 3: no such file. Any other failure is a file that is there but cannot be read, never one that is absent.
READ_FILE = '[ -e "$1" ] || [ -L "$1" ] || exit 3; stat -c "%U:%G %a" "$1" && cat "$1"'
# A new file is whole or absent, never half written: a broken sudoers file stops every sudo on the machine.
WRITE_FILE = (
    'umask 077; t=$(mktemp "$1.XXXXXX") || exit 1; trap \'rm -f "$t"\' EXIT; '
    'cat > "$t" && chown "$2" "$t" && chmod "$3" "$t" && { [ -z "$4" ] || $4 "$t"; } && mv -f "$t" "$1"'
)
WRITE_IN_PLACE = 'umask 077; cat > "$1" && chown "$2" "$1" && chmod "$3" "$1"'
READ_DIRS = 'for d in "$@"; do stat -c "%n %U:%G %a" "$d" 2>/dev/null || echo "$d missing"; done'
MAKE_DIR = 'mkdir -p "$1" && chown "$2" "$1" && chmod "$3" "$1"'


class SetupError(Exception):
    """The set-up stopped; ``problems`` says what was found and what the owner does about it."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


def said(done: subprocess.CompletedProcess[str]) -> str:
    return (done.stderr.strip() or done.stdout.strip() or f"exit code {done.returncode}").splitlines()[-1]


@dataclass
class Host:
    """Every command on the server goes through ``run``; a dry run reads but never changes."""

    run: Run
    dry_run: bool
    changes: list[str] = field(default_factory=list)

    def read(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        return self.run(argv, None)

    def must(self, argv: list[str], what: str) -> str:
        done = self.run(argv, None)
        if done.returncode != 0:
            raise SetupError([f"{what}: {said(done)}"])
        return done.stdout

    def act(self, what: str, do: Callable[[], object]) -> None:
        self.changes.append(what)
        print(f"   {'would' if self.dry_run else 'changed:'} {what}")
        if not self.dry_run:
            do()

    def change(self, what: str, argv: list[str], stdin: str | None = None) -> None:
        def do() -> None:
            done = self.run(argv, stdin)
            if done.returncode != 0:
                raise SetupError([f"{what}: {said(done)}"])

        self.act(what, do)


def ssh_runner(config: Config, known_hosts: Path) -> Run:
    server = config.server
    base = ["ssh", "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known_hosts}"]
    base += ["-o", "GlobalKnownHostsFile=/dev/null", "-o", f"HostKeyAlias={server.host}", "-o", "BatchMode=yes"]
    base += ["-o", "ConnectTimeout=20", f"{server.admin}@{server.host}", "--"]

    def remote(argv: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
        feed: dict[str, Any] = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
        try:
            done = subprocess.run(
                [*base, shlex.join(argv)], capture_output=True, text=True, timeout=SSH_SECONDS, check=False, **feed
            )
        except subprocess.TimeoutExpired as exc:
            raise SetupError([f"`{shlex.join(argv)}` on the server took over {SSH_SECONDS} s"]) from exc
        if done.returncode == 255:
            raise SetupError([f"cannot reach {server.admin}@{server.host} over SSH: {done.stderr.strip()}"])
        return done

    return remote


def remote_text(host: Host, path: str) -> tuple[str, str] | None:
    """``("owner:group mode", content)`` of a file on the server, or None when it is not there. A file that is
    there but cannot be read stops the set-up: read as absent, it would be written over (its secrets made anew)."""
    done = host.read(["sudo", "-n", "sh", "-c", READ_FILE, "read", path])
    if done.returncode == 3:
        return None
    if done.returncode != 0:
        raise SetupError([f"{path} is on the server but cannot be read: {said(done)}"])
    stat, _, text = done.stdout.partition("\n")
    return stat, text


def ensure_file(
    host: Host, path: str, text: str, *, owner: str, mode: str, in_place: bool = False, check: str = ""
) -> bool:
    """Makes ``path`` hold ``text`` with that owner and mode; True when it had to change."""
    found = remote_text(host, path)
    if found == (f"{owner} {mode}", text):
        print(f"   found: {path}")
        return False
    print(f"   found: {path} " + ("missing" if found is None else f"different ({found[0]})"))
    script = WRITE_IN_PLACE if in_place else WRITE_FILE
    host.change(f"write {path}", ["sudo", "-n", "sh", "-c", script, "write", path, owner, mode, check], text)
    return True


def ensure_dirs(host: Host, wanted: list[tuple[str, str, str]]) -> None:
    """Makes each (path, owner:group, mode) folder exist as given."""
    listed = host.read(["sudo", "-n", "sh", "-c", READ_DIRS, "dirs", *[path for path, _, _ in wanted]]).stdout
    found = dict(line.split(" ", 1) for line in listed.splitlines() if " " in line)
    right = [path for path, owner, mode in wanted if found.get(path) == f"{owner} {mode}"]
    if right:
        print(f"   found: {', '.join(right) if len(right) < 3 else f'{len(right)} folders'} as they should be")
    for path, owner, mode in wanted:
        if path not in right:
            print(f"   found: {path} {found.get(path, 'missing')}")
            make = ["sudo", "-n", "sh", "-c", MAKE_DIR, "mkdir", path, owner, mode]
            host.change(f"make {path} ({owner} {mode})", make)


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    found: str
    todo: str  # what the owner does when it fails
    fix: tuple[tuple[str, list[str]], ...] = ()  # what --fresh runs; empty when it is a person's step


def _firewall(host: Host) -> Check:
    done = host.read(UFW)
    todo = "make ufw allow exactly 22, 80 and 443 inbound (22 first), then `ufw --force enable`"
    fix = (
        ("update the package lists", [*_APT, "update"]),
        ("install ufw", [*_APT, "install", "-y", "ufw"]),
        *((f"allow {port}/tcp", ["sudo", "-n", "ufw", "allow", f"{port}/tcp"]) for port in OPEN_PORTS),
        ("enable the firewall", ["sudo", "-n", "ufw", "--force", "enable"]),
    )
    if done.returncode != 0:
        return Check("firewall", False, f"ufw cannot be read ({said(done)})", todo, fix)
    active = "Status: active" in done.stdout.splitlines()
    rules = [line.split(" ALLOW")[0] for line in done.stdout.splitlines() if " ALLOW" in line]
    allowed = sorted({rule.replace("(v6)", "").strip().removesuffix("/tcp") for rule in rules}, key=_by_port)
    found = f"ufw {'active' if active else 'inactive'}, allowing {', '.join(allowed) or 'nothing'}"
    return Check("firewall", active and allowed == list(OPEN_PORTS), found, todo, fix)


def _by_port(rule: str) -> str:
    return rule.zfill(8)


def _sshd(host: Host, config: Config) -> Check:
    done = host.read(SSHD)
    todo = (
        f"check from another terminal that `ssh {config.server.admin}@{config.server.host}` signs in with your key, "
        "then run the set-up again with --lockdown"
    )
    if done.returncode != 0:
        return Check("sshd", False, f"sshd's settings cannot be read ({said(done)})", todo)
    settings = dict(line.split(" ", 1) for line in done.stdout.splitlines() if " " in line)
    keys = ("passwordauthentication", "kbdinteractiveauthentication", "permitrootlogin")
    wrong = [f"{key} {settings.get(key, '(not set)')}" for key in keys if settings.get(key) != "no"]
    found = "sshd takes keys only, no root" if not wrong else "sshd has " + ", ".join(wrong)
    return Check("sshd", not wrong, found, todo)


def _tailscale(host: Host, config: Config) -> Check:
    done = host.read(TAILSCALE)
    name = config.server.tailnet
    todo = (
        f"on the server, `sudo tailscale up --hostname={name.split('.')[0]}` so it joins the tailnet as {name} "
        "(MagicDNS and HTTPS certificates on in the tailnet's DNS settings), then run the set-up again"
    )
    install = (("install Tailscale", ["sudo", "-n", "sh", "-c", "curl -fsSL https://tailscale.com/install.sh | sh"]),)
    if done.returncode == 3:
        return Check("tailscale", False, "Tailscale is not installed", todo, install)
    if done.returncode != 0:
        return Check("tailscale", False, f"Tailscale is installed but not running ({said(done)})", todo)
    status = json.loads(done.stdout)
    state, me = status.get("BackendState"), (status.get("Self") or {}).get("DNSName", "").rstrip(".")
    ok = state == "Running" and me == name
    return Check("tailscale", ok, f"Tailscale {state}, joined as {me or '(nothing)'}", todo)


def _swap(host: Host) -> Check:
    done = host.read(SWAP)
    found = done.stdout.split()[:3] if done.returncode == 0 else []
    swap = (("add a 4G swapfile", ["sudo", "-n", "sh", "-c", _SWAPFILE]),)
    todo = "add swap (a 4G /swapfile), so running out of memory slows the server down instead of killing a database"
    return Check("swap", bool(found), "swap " + (" ".join(found) if found else "none"), todo, swap)


def _docker(host: Host) -> Check:
    done = host.read(DOCKER)
    todo = "install Docker and its compose plugin (Docker's apt repository)"
    fix = ((todo, ["sudo", "-n", "sh", "-c", _DOCKER_REPO]),)
    if done.returncode != 0:
        return Check("docker", False, f"Docker is not ready ({said(done)})", todo, fix)
    return Check("docker", True, "Docker " + ", compose ".join(done.stdout.split()), todo, fix)


def python_floor() -> tuple[int, ...]:
    """The oldest Python the factory runs on, as requires-python in its pyproject.toml declares it (>=X.Y)."""
    try:
        declared = tomllib.loads((FACTORY / "pyproject.toml").read_text())["project"]["requires-python"]
    except (OSError, tomllib.TOMLDecodeError, KeyError) as exc:
        raise SetupError([f"the factory's pyproject.toml declares no requires-python: {exc!r}"]) from exc
    if not isinstance(declared, str) or not re.fullmatch(r">=\d+(\.\d+)*", declared):
        raise SetupError([f"the factory's requires-python {declared!r} is not a plain >=X.Y floor"])
    return tuple(int(part) for part in declared[2:].split("."))


def _python(host: Host) -> Check:
    done = host.read(PYTHON)
    floor = python_floor()
    want = ".".join(map(str, floor))
    todo = f"install Python {want} or newer as /usr/bin/python3 (Ubuntu 24.04 and Debian 13 have it)"
    found = tuple(int(part) for part in done.stdout.strip().split(".")) if done.returncode == 0 else ()
    if not found:
        return Check("python", False, f"no /usr/bin/python3 answers ({said(done)})", todo)
    return Check("python", found >= floor, f"Python {done.stdout.strip()}", todo)


def checks(host: Host, config: Config) -> list[Check]:
    found = [_firewall(host), _sshd(host, config), _tailscale(host, config), _swap(host), _docker(host),
             _python(host)]  # fmt: skip
    for item in found:
        print(f"   found: {item.found}")
    return found


def check(host: Host, config: Config, *, fresh: bool) -> list[str]:
    """Checks the machine; with ``fresh`` it sets up what it can. Returns what the owner still does."""
    failing = [item for item in checks(host, config) if not item.ok]
    if not fresh:
        if failing:
            raise SetupError(
                ["this server is not ready, and the set-up changes none of this on an existing machine:"]
                + [f"{item.name}: {item.found}. To do: {item.todo}" for item in failing]
            )
        return []
    fixes = [step for item in failing for step in item.fix]
    for what, argv in fixes:
        host.change(what, argv)
    if fixes and not host.dry_run:
        failing = [item for item in checks(host, config) if not item.ok]
    later = [item.todo for item in failing if item.name == "sshd"]
    stopping = [item for item in failing if item.name != "sshd"]
    if stopping and not host.dry_run:
        raise SetupError([f"{item.name}: {item.found}. To do: {item.todo}" for item in stopping])
    return later


def lockdown(host: Host, config: Config) -> None:
    """sshd takes keys only and refuses root; validated before sshd restarts, removed again if it is refused."""
    found = _sshd(host, config)
    print(f"   found: {found.found}")
    current = remote_text(host, LOCKDOWN)
    if found.ok and current == ("root:root 644", LOCKDOWN_TEXT):
        print("Already locked down: changed nothing.")
        return
    script = ["sudo", "-n", "sh", "-c", _LOCK, "lock", LOCKDOWN]
    host.change(f"write {LOCKDOWN} and restart sshd", script, LOCKDOWN_TEXT)
    if host.dry_run:
        return
    after = _sshd(host, config)
    if not after.ok:
        raise SetupError([f"after the lockdown, {after.found}: another sshd setting overrides it"])
    print(f"Locked down: {after.found}. Sign in as {config.server.admin} from now on.")
