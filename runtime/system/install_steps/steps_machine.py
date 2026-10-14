"""The steps that set up the machine: packages, the four users, the state folder, the read-only install, the
agents' Claude Code, bubblewrap's AppArmor profile, the two checkouts and the stack's rootless Docker.

Each one may run again after any failure and finish the job: what is in place already is kept, and what a stopped run
left half made is made again.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from runtime.shared import network
from runtime.shared.paths import AGENT_USER, DASHBOARD_USER, WARDEN_USER, queue_dir
from runtime.shared.settings import overrides_toml
from runtime.system import layout, render
from runtime.system.host import CommandError, Host
from runtime.system.install_steps import steps_wire
from runtime.system.job import Install, refuse
from runtime.warden.sessions import session_settings

# Claude Code carries its seccomp helper (which blocks Unix sockets in the sandbox) inside its own binary; a version
# without it finds the sandbox runtime's copy in the global npm folder instead.
_SECCOMP_MARK = b"apply-seccomp"
_NPM_RUNTIME = (
    Path("/usr/lib/node_modules/@anthropic-ai/sandbox-runtime"),
    Path("/usr/local/lib/node_modules/@anthropic-ai/sandbox-runtime"),
)
_CHUNK = 1 << 20
_VERSION = re.compile(r"^(\d+(?:\.\d+){1,3})\b")
_APT_ENV = {"DEBIAN_FRONTEND": "noninteractive"}
_SUBORDINATE_START, _SUBORDINATE_COUNT = 100_000, 65_536  # where useradd starts subordinate ids, and how many


def installed(host: Host, package: str) -> bool:
    return "install ok installed" in host.query(["dpkg-query", "-W", "-f=${Status}", package])[1]


def packages(ctx: Install) -> None:
    """Only what is missing; apt is not run at all when every package is installed. Docker's rootless extras come
    along when Docker's own packages are installed without them."""
    host = ctx.host
    wanted = list(layout.PACKAGES)
    if host.query(["which", layout.ROOTLESS_SETUP])[0] != 0 and installed(host, "docker-ce"):
        wanted.append(layout.ROOTLESS_EXTRAS)
    missing = [name for name in wanted if not installed(host, name)]
    if not missing:
        host.say("every package is installed")
        return
    apt = ["apt-get", "install", "-y", "--no-install-recommends", *missing]
    try:
        host.run(apt, env=_APT_ENV)
    except CommandError:
        # telemetry: expected — a package list older than the archive names dropped files: refreshed, it works
        host.run(["apt-get", "update"], env=_APT_ENV)
        host.run(apt, env=_APT_ENV)


def _useradd(name: str, ctx: Install) -> list[str]:
    nologin = ["--shell", "/usr/sbin/nologin", "--user-group"]
    if name == WARDEN_USER:
        return ["useradd", "--system", "--home-dir", str(ctx.places.state), "--no-create-home", *nologin, name]
    if name == DASHBOARD_USER:
        return ["useradd", "--system", "--home-dir", "/nonexistent", "--no-create-home", *nologin, name]
    if name == AGENT_USER:
        # A regular user, so Claude Code's shell has a login shell and a home of its own.
        return ["useradd", "--create-home", "--home-dir", str(layout.AGENT_HOME), "--shell", "/bin/bash",
                "--user-group", name]  # fmt: skip
    # A regular user gets subordinate ids, which rootless Docker maps a container's users to.
    return ["useradd", "--create-home", "--home-dir", str(layout.DOCKER_USERS[name]), *nologin, name]


def users(ctx: Install) -> None:
    host = ctx.host
    if host.query(["getent", "group", layout.SLOTS_GROUP])[0] != 0:
        host.run(["groupadd", "--system", layout.SLOTS_GROUP])
    _accounts(ctx)
    # The stack user's rootless Docker reads a slot's checkout, in the agent's home, for bind mounts and build
    # contexts. The daemon runs with the account's own groups, never a unit's, so the membership is the account's.
    stack = host.account(layout.STACK_USER)
    if stack is None or AGENT_USER not in stack.groups:
        host.run(["gpasswd", "-a", layout.STACK_USER, AGENT_USER])
    host.run(["chmod", "0750", str(layout.AGENT_HOME)])
    host.mkdir(layout.AGENT_HOME / ".claude", mode=0o700, owner=AGENT_USER)
    _agent_caches(ctx)
    for name in (AGENT_USER, *layout.DOCKER_USERS):
        host.run(["loginctl", "enable-linger", name])


def _accounts(ctx: Install) -> None:
    """Each factory user made, or kept with its home checked and its privileged groups taken away."""
    host = ctx.host
    homes = {AGENT_USER: layout.AGENT_HOME, **layout.DOCKER_USERS}
    for name in layout.USERS:
        account = host.account(name)
        if account is None:
            host.run(_useradd(name, ctx))
        else:
            if name in homes and account.home != homes[name]:
                raise refuse(f"This computer already has a user {name} whose home is not {homes[name]}: uninstall "
                             "the factory, which removes that user, then install again")  # fmt: skip
            # A factory user in any of these could reach root, Docker, the journal, a disk or the owner's files.
            for group in sorted((layout.PRIVILEGED_GROUPS | {owner_group(ctx)}) & set(account.groups)):
                host.run(["gpasswd", "--delete", name, group])
        if name in homes:
            host.run(["passwd", "--lock", name])


def _agent_caches(ctx: Install) -> None:
    """A session cannot make its tools' caches itself: it may write in each one, never in the home around it. Every
    folder on the way is the agent's too, so a tool run outside the sandbox can still add its own beside them."""
    folders: dict[Path, None] = {}
    for cache in ctx.need_settings().get_list("sandbox.writable_caches"):
        parts = Path(cache).parts
        folders.update({layout.AGENT_HOME.joinpath(*parts[: n + 1]): None for n in range(len(parts))})
    for folder in folders:
        ctx.host.mkdir(folder, mode=0o700, owner=AGENT_USER)


def owner_group(ctx: Install) -> str:
    """The owner's primary group: an account lists it first."""
    if not ctx.owner.groups:
        raise refuse(f"{ctx.owner.name}'s primary group cannot be read: the state folder cannot be given to it")
    return ctx.owner.groups[0]


def state_tree(ctx: Install) -> None:
    host, state = ctx.host, ctx.places.state
    # The owner may pass through (never list) to public/: the factory's git hooks in the owner's own checkout read the
    # settings the warden publishes there. No factory user is in the owner's group, so to them the folder stays shut.
    host.mkdir(state, mode=0o710, owner=WARDEN_USER, group=owner_group(ctx))
    public = layout.public(state)
    rendered = (ctx.places.rendered_plugin, ctx.places.rendered_agents)
    for folder in (public, public / "launch", session_settings.folder(ctx.places), *rendered):
        host.mkdir(folder, mode=0o755, owner=WARDEN_USER)
    for folder in (layout.secrets(state), layout.runs(state), ctx.places.overrides.parent):
        host.mkdir(folder, mode=0o700, owner=WARDEN_USER)
    # setgid: each slot's folder takes the slots' group, which the stack user's runs join.
    host.mkdir(ctx.places.slots, mode=0o2750, owner=WARDEN_USER, group=layout.SLOTS_GROUP)
    # Each slot's env file and provisioning state, written by the stack user's runs; no other user may list it.
    host.mkdir(ctx.places.stack_slots, mode=0o700, owner=layout.STACK_USER)
    host.write(ctx.places.overrides, overrides_toml(ctx.overrides), mode=0o600, owner=WARDEN_USER)
    host.write(ctx.places.owner, json.dumps({"user": ctx.owner.name}) + "\n", mode=0o644, owner=WARDEN_USER)


def in_place(ctx: Install) -> bool:
    """The commit is installed already, unchanged since the install that recorded it."""
    host, commit = ctx.host, ctx.places.install / "COMMIT"
    if host.dry or not host.exists(commit) or not host.exists(ctx.places.install_hash):
        return False
    if host.read(commit).strip() != ctx.sha:
        return False
    return steps_wire.install_digest(ctx) == host.read(ctx.places.install_hash).strip()


def place(ctx: Install) -> None:
    """The sealed tree, root-owned and read-only, swapped in whole, unless that commit is in place already; then the
    pinned check tools beside it."""
    host, install = ctx.host, ctx.places.install
    fresh, old = install.with_name(install.name + ".new"), install.with_name(install.name + ".old")
    for leftover in (fresh, old):
        if host.exists(leftover):
            host.remove(leftover)
    if in_place(ctx):
        host.say(f"the factory at {ctx.sha[:12]} is in place already, unchanged: kept")
    else:
        host.copy_tree(ctx.tree(), fresh)
        # The release the warden's and the dashboard's own error reports name.
        host.write(fresh / "COMMIT", ctx.sha + "\n", mode=0o644)
        host.run(["chown", "-R", "root:root", str(fresh)])
        host.run(["chmod", "-R", "u=rwX,go=rX", str(fresh)])
        if host.exists(install):
            host.run(["mv", str(install), str(old)])
        host.run(["mv", str(fresh), str(install)])
        if host.exists(old):
            host.remove(old)
    host.run(
        [ctx.tool("uv"), "sync", "--frozen", "--extra", "dev", "--no-install-project", "--project", str(install),
         "--python", layout.PYTHON],
        env={"UV_PROJECT_ENVIRONMENT": str(layout.TOOLS)},
    )  # fmt: skip
    host.run(["chmod", "-R", "u=rwX,go=rX", str(layout.TOOLS)])


def embeds_seccomp(binary: Path) -> bool:
    tail = b""
    with binary.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            if _SECCOMP_MARK in tail + chunk:
                return True
            tail = chunk[-len(_SECCOMP_MARK) :]
    return False


def _version(text: str) -> tuple[int, ...] | None:
    found = _VERSION.match(text.strip())
    return tuple(int(part) for part in found.group(1).split(".")) if found else None


def claude_version(ctx: Install) -> None:
    """The factory reads Claude Code's own files and output (its session records, its launch line, its transcripts),
    which no version promises to keep: only a version the drills passed with is installed for the agents."""
    host, settings = ctx.host, ctx.need_settings()
    lowest, untested = settings.get_str("install.claude_lowest"), settings.get_str("install.claude_untested")
    code, said = host.query([ctx.tool("claude"), "--version"], user=ctx.owner.name)
    if host.dry:
        host.say(f"Claude Code: yours must be at least {lowest} and below {untested} (a dry run does not ask)")
        return
    have, low, high = _version(said), _version(lowest), _version(untested)
    if low is None or high is None:
        raise refuse("The settings install.claude_lowest and install.claude_untested must each be a version such as "
                     "2.1.296: fix them with --set, then install again")  # fmt: skip
    if code != 0 or have is None:
        raise refuse(f"Your Claude Code did not say its version ({said.strip()[:80]!r}): update it, then install again")
    if not low <= have < high:
        raise refuse(f"Your Claude Code is {'.'.join(map(str, have))}, and the factory is drilled with {lowest} up "
                     f"to below {untested}: install one of those (claude install {lowest}), or, to try this one, "
                     "install again with --set install.claude_untested=<a higher version>, and check every drill "
                     "passes")  # fmt: skip
    host.say(f"Claude Code {'.'.join(map(str, have))}: within the versions the drills passed with")


def claude_binary(ctx: Install) -> None:
    host = ctx.host
    claude_version(ctx)
    host.mkdir(layout.CLAUDE_BIN.parent, mode=0o755)
    host.run(["install", "-m", "0755", "-o", "root", "-g", "root", ctx.tool("claude"), str(layout.CLAUDE_BIN)])
    host.say(f"{host.query([str(layout.CLAUDE_BIN), '--version'])[1].strip() or 'Claude Code'} for the agents")
    if host.dry or embeds_seccomp(layout.CLAUDE_BIN) or any(host.exists(p) for p in _NPM_RUNTIME):
        return
    raise refuse(
        "Your Claude Code is too old to keep the agents' sandbox shut: update it (claude update), then install again"
    )


def apparmor(ctx: Install) -> None:
    host = ctx.host
    if not host.exists(layout.APPARMOR_DIR):
        host.say("no AppArmor on this machine: bubblewrap needs no profile")
        return
    pattern = rf"^\s*profile\s+\S+\s+{layout.BWRAP}\b"
    code, out = host.query(["grep", "-rlE", pattern, str(layout.APPARMOR_DIR)])
    others = [line for line in out.splitlines() if line.strip() and line.strip() != str(layout.APPARMOR_PROFILE)]
    if code == 0 and others:
        host.say(f"{others[0]} already lets bubblewrap run")
        return
    host.write(layout.APPARMOR_PROFILE, render.artifact(render.APPARMOR, ctx.templates(), {"bwrap": str(layout.BWRAP)}),
               mode=render.APPARMOR.mode)  # fmt: skip
    host.run(["apparmor_parser", "-r", str(layout.APPARMOR_PROFILE)])


def _clone(ctx: Install, source: Path, dest: Path, origin: str, owner: str, branch: str) -> None:
    """A clone of the bundle of history ``source`` at ``dest``, on ``branch``, owned by ``owner``. It is made beside
    ``dest`` and moved there whole, so a stopped run never leaves half a clone."""
    host = ctx.host
    if host.exists(dest):
        host.say(f"{dest} is already there; kept as it is")
        return
    partial = dest.with_name(dest.name + ".partial")
    if host.exists(partial):
        host.remove(partial)
    host.run(["git", "clone", "--quiet", str(source), str(partial)])
    host.run(["git", "-C", str(partial), "checkout", "-q", "-B", branch, "--track", f"origin/{branch}"])
    host.run(["git", "-C", str(partial), "remote", "set-url", "origin", origin])
    host.run(["chown", "-R", f"{owner}:{owner}", str(partial)])
    host.run(["mv", str(partial), str(dest)])


def _clone_checkout(ctx: Install, checkout: Path, dest: Path, origin: str, owner: str, branch: str) -> None:
    """``_clone`` of the owner's ``checkout``. Git refuses root a repository another user owns, and a -c
    safe.directory never reaches the upload-pack a local clone starts, so the owner writes the checkout's history
    as a bundle and root clones that: root never opens the owner's repository."""
    host = ctx.host
    if host.exists(dest):
        host.say(f"{dest} is already there; kept as it is")
        return
    bundle = dest.with_name(dest.name + ".bundle")
    try:
        host.pipe(["git", "-C", str(checkout), "bundle", "create", "-", "--all"], ctx.owner.name,
                  ["dd", f"of={bundle}", "status=none"])  # fmt: skip
        _clone(ctx, bundle, dest, origin, owner, branch)
    finally:
        host.remove(bundle)


def agent_checkout(ctx: Install) -> None:
    """The agent user's own checkout of the product, on its default branch, and in it the warden's queue; the owner
    keeps theirs."""
    branch = ctx.need_settings().get_str("repo.default_branch")
    _clone_checkout(ctx, ctx.product, ctx.checkout(), ctx.product_origin, AGENT_USER, branch)
    # A folder only the warden writes, in a checkout the agent owns, so only root can make it. A queue that is not a
    # folder is refused, never followed: root would give the warden whatever a link there points to.
    queue = queue_dir(ctx.checkout())
    code, kind = ctx.host.query(["stat", "-c", "%F", str(queue)])
    if code == 0 and kind.strip() and kind.strip() != "directory":
        raise refuse(f"{queue} is a {kind.strip()}, not a folder: remove it, then install again")
    ctx.host.mkdir(queue, mode=0o755, owner=WARDEN_USER)
    # A slot's containers read the worktrees as users outside the agent's group, so what agents wrote before their
    # umask let others read it is opened up here. Run as the agent, it changes only the agent's own files; it never
    # follows a link, and never enters a folder another user owns (a container's, say), which it could not read.
    ctx.host.run(["find", str(ctx.checkout()), "(", "-name", ".git", "-o", "!", "-user", AGENT_USER, ")", "-prune",
                  "-o", "(", "-type", "f", "-o", "-type", "d", ")", "-exec", "chmod", "o+rX", "{}", "+"],
                 user=AGENT_USER)  # fmt: skip
    # Claude Code starts no session in an untrusted folder, and delivers no message while its first run is not done.
    trust = ctx.places.install / "runtime" / "system" / "runners" / "trust.py"  # placed by the step before
    config = layout.AGENT_HOME / ".claude.json"
    ctx.host.run([layout.PYTHON, "-I", str(trust), str(config), str(ctx.checkout())], user=AGENT_USER)


_HOST_KEY = re.compile(r"^(ssh-ed25519|ecdsa-sha2-nistp(?:256|384|521)|ssh-rsa) [A-Za-z0-9+/=]+$")
# GitHub's own list of its SSH host keys, read over HTTPS: the certificate vouches for them, so nothing is trusted on
# first use.
GITHUB_META = "https://api.github.com/meta"


def _get(url: str, timeout: float) -> bytes:
    return network.call(url, timeout, follow=True).checked("GitHub").body


def github_host_keys(ctx: Install) -> None:
    """github.com's SSH host keys for the warden's pushes and its mirror's fetches."""
    host, path = ctx.host, ctx.places.github_known_hosts
    if host.dry:
        host.say(f"would write {path} from GitHub's published SSH host keys ({GITHUB_META})")
        return
    try:
        keys = json.loads(_get(GITHUB_META, ctx.need_settings().get_int("watch.http_timeout_seconds")))["ssh_keys"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise refuse(f"GitHub's SSH host keys could not be read from {GITHUB_META} ({type(exc).__name__}): check "
                     "this computer's internet connection, then install again") from exc  # fmt: skip
    if not isinstance(keys, list) or not keys or not all(isinstance(k, str) and _HOST_KEY.match(k) for k in keys):
        raise refuse(f"GitHub named no SSH host keys this install can read at {GITHUB_META}: install again later")
    host.mkdir(path.parent, mode=0o700, owner=WARDEN_USER)
    host.write(path, "".join(f"github.com {key}\n" for key in keys), mode=0o644, owner=WARDEN_USER)


def _subordinate_ids(ctx: Install, user: str) -> None:
    """Give a Docker user the subordinate ids rootless Docker maps a container's users to, past every range already
    given, when useradd gave it none."""
    host = ctx.host
    flags = []
    for table, flag in (("/etc/subuid", "--add-subuids"), ("/etc/subgid", "--add-subgids")):
        if host.query(["grep", "-q", f"^{user}:", table])[0] == 0:
            continue
        ends = [_SUBORDINATE_START]
        for line in host.read(Path(table)).splitlines():
            fields = line.strip().split(":")
            if len(fields) == 3 and fields[1].isdigit() and fields[2].isdigit():
                ends.append(int(fields[1]) + int(fields[2]))
        start = max(ends)
        flags += [flag, f"{start}-{start + _SUBORDINATE_COUNT - 1}"]
    if flags:
        host.run(["usermod", *flags, user])


def _stack_wall(ctx: Install) -> None:
    """The stack user's systemd is walled off the tailnet before it starts. One already running when its wall
    changes restarts, so the kernel holds every container it runs from here on; the shared services start later."""
    host, values = ctx.host, steps_wire.given(ctx)
    wall = render.stack_wall(values["stack_uid"])
    text = render.artifact(wall, ctx.templates(), values)
    if host.exists(wall.dest) and host.read(wall.dest) == text:
        return
    host.mkdir(wall.dest.parent, mode=0o755)
    host.write(wall.dest, text, mode=wall.mode)
    host.run(["systemctl", "daemon-reload"])
    unit = f"user@{values['stack_uid']}.service"
    if host.query(["systemctl", "is-active", "--quiet", unit])[0] == 0:
        host.run(["systemctl", "restart", unit])


def rootless_docker(ctx: Install) -> None:
    """Each Docker user's own Docker daemon, the stack user's walled off the tailnet first: a container's root is that
    user on the host, never root."""
    host = ctx.host
    if host.query(["which", layout.ROOTLESS_SETUP])[0] != 0 and not host.dry:
        raise refuse("Docker's rootless extras are missing on this computer: install Docker from Docker's own "
                     "packages (docker-ce and docker-ce-rootless-extras), then install again")  # fmt: skip
    _stack_wall(ctx)
    for user, home in layout.DOCKER_USERS.items():
        if not host.dry:
            _subordinate_ids(ctx, user)
        host.run(["systemctl", "start", f"user@{ctx.uid(user)}.service"])
        if host.exists(home / ".config" / "systemd" / "user" / "docker.service"):
            host.say(f"{user}'s rootless Docker is already set up")
        else:
            host.run([layout.ROOTLESS_SETUP, "install"], user=user)
        host.run(["systemctl", "--user", "enable", "--now", "docker.service"], user=user)
