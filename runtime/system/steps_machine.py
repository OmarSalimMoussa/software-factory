"""The steps that set up the machine: packages, the four users, the state folder, the read-only install, the
agents' Claude Code, bubblewrap's AppArmor profile, the two checkouts, the stack's rootless Docker and the secrets."""

from __future__ import annotations

import json
from pathlib import Path

from runtime.shared import claude_accounts, totp
from runtime.shared.claude_accounts import AccountError
from runtime.shared.paths import AGENT_USER, DASHBOARD_USER, WARDEN_USER
from runtime.shared.settings import overrides_toml
from runtime.system import github_keys, layout, render
from runtime.system.host import DeployKey
from runtime.system.job import Install, github_name, refuse

# Claude Code carries its seccomp helper (which blocks Unix sockets in the sandbox) inside its own binary; a version
# without it finds the sandbox runtime's copy in the global npm folder instead.
_SECCOMP_MARK = b"apply-seccomp"
_NPM_RUNTIME = (
    Path("/usr/lib/node_modules/@anthropic-ai/sandbox-runtime"),
    Path("/usr/local/lib/node_modules/@anthropic-ai/sandbox-runtime"),
)
_CHUNK = 1 << 20
# Each key's comment, which GitHub shows as its title; the uninstaller names the first when it says what is left.
DEPLOY_TITLE = "software-factory deploy"
READ_TITLE = "software-factory read"
READ_SECRET = "FACTORY_READ_KEY"  # the secret the CI templates check the factory out with


def packages(ctx: Install) -> None:
    missing = [
        name for name in layout.PACKAGES
        if "install ok installed" not in ctx.host.query(["dpkg-query", "-W", "-f=${Status}", name])[1]
    ]  # fmt: skip
    if missing:
        ctx.host.run(["apt-get", "install", "-y", "--no-install-recommends", *missing])
    else:
        ctx.host.say("  every package is installed")


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
    return ["useradd", "--create-home", "--home-dir", str(layout.STACK_HOME), *nologin, name]


def users(ctx: Install) -> None:
    host = ctx.host
    if host.query(["getent", "group", layout.SLOTS_GROUP])[0] != 0:
        host.run(["groupadd", "--system", layout.SLOTS_GROUP])
    homes = {AGENT_USER: layout.AGENT_HOME, layout.STACK_USER: layout.STACK_HOME}
    for name in layout.USERS:
        account = host.account(name)
        if account is None:
            host.run(_useradd(name, ctx))
            if name in homes:
                host.run(["passwd", "--lock", name])
            continue
        privileged = sorted((layout.PRIVILEGED_GROUPS | {owner_group(ctx)}) & set(account.groups))
        if privileged:
            raise refuse(f"{name} is in {', '.join(privileged)}: remove it (gpasswd -d {name} <group>), then reinstall")
        if name in homes and account.home != homes[name]:
            raise refuse(f"{name}'s home is {account.home}, not {homes[name]}: the units bind that path")
    # The stack user's rootless Docker reads a slot's checkout, in the agent's home, for bind mounts and build
    # contexts. The daemon runs with the account's own groups, never a unit's, so the membership is the account's.
    stack = host.account(layout.STACK_USER)
    if stack is None or AGENT_USER not in stack.groups:
        host.run(["gpasswd", "-a", layout.STACK_USER, AGENT_USER])
    host.run(["chmod", "0750", str(layout.AGENT_HOME)])
    host.mkdir(layout.AGENT_HOME / ".claude", mode=0o700, owner=AGENT_USER)
    # A session cannot make its tools' caches itself: it may write in each one, never in the home around it. Every
    # folder on the way is the agent's too, so a tool run outside the sandbox can still add its own beside them.
    folders: dict[Path, None] = {}
    for cache in ctx.need_settings().get_list("sandbox.writable_caches"):
        parts = Path(cache).parts
        folders.update({layout.AGENT_HOME.joinpath(*parts[: n + 1]): None for n in range(len(parts))})
    for folder in folders:
        host.mkdir(folder, mode=0o700, owner=AGENT_USER)
    for name in (AGENT_USER, layout.STACK_USER):
        host.run(["loginctl", "enable-linger", name])


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
    for folder in (public, public / "launch", ctx.places.rendered_plugin):
        host.mkdir(folder, mode=0o755, owner=WARDEN_USER)
    for folder in (layout.secrets(state), layout.runs(state), ctx.places.overrides.parent):
        host.mkdir(folder, mode=0o700, owner=WARDEN_USER)
    # setgid: each slot's folder takes the slots' group, which the stack user's runs join.
    host.mkdir(ctx.places.slots, mode=0o2750, owner=WARDEN_USER, group=layout.SLOTS_GROUP)
    # Each slot's env file and provisioning state, written by the stack user's runs; no other user may list it.
    host.mkdir(ctx.places.stack_slots, mode=0o700, owner=layout.STACK_USER)
    host.write(ctx.places.overrides, overrides_toml(ctx.overrides), mode=0o600, owner=WARDEN_USER)
    host.write(ctx.places.owner, json.dumps({"user": ctx.owner.name}) + "\n", mode=0o644, owner=WARDEN_USER)


def place(ctx: Install) -> None:
    """The archived tree, root-owned and read-only, swapped in whole; then the pinned check tools beside it."""
    host, install = ctx.host, ctx.places.install
    fresh, old = install.with_name(install.name + ".new"), install.with_name(install.name + ".old")
    for leftover in (fresh, old):
        if host.exists(leftover):
            host.remove(leftover)
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


def claude_binary(ctx: Install) -> None:
    host = ctx.host
    host.mkdir(layout.CLAUDE_BIN.parent, mode=0o755)
    host.run(["install", "-m", "0755", "-o", "root", "-g", "root", ctx.tool("claude"), str(layout.CLAUDE_BIN)])
    host.say(f"  {host.query([str(layout.CLAUDE_BIN), '--version'])[1].strip() or 'Claude Code'} for the agents")
    if host.dry or embeds_seccomp(layout.CLAUDE_BIN) or any(host.exists(p) for p in _NPM_RUNTIME):
        return
    raise refuse(
        "this Claude Code carries no seccomp helper, so its sandbox could not block Unix sockets: run "
        "`npm install -g @anthropic-ai/sandbox-runtime`, then install again"
    )


def apparmor(ctx: Install) -> None:
    host = ctx.host
    if not host.exists(layout.APPARMOR_DIR):
        host.say("  no AppArmor on this machine: bubblewrap needs no profile")
        return
    pattern = rf"^\s*profile\s+\S+\s+{layout.BWRAP}\b"
    code, out = host.query(["grep", "-rlE", pattern, str(layout.APPARMOR_DIR)])
    others = [line for line in out.splitlines() if line.strip() and line.strip() != str(layout.APPARMOR_PROFILE)]
    if code == 0 and others:
        host.say(f"  {others[0]} already lets bubblewrap run")
        return
    host.write(layout.APPARMOR_PROFILE, render.artifact(render.APPARMOR, ctx.templates(), {"bwrap": str(layout.BWRAP)}),
               mode=render.APPARMOR.mode)  # fmt: skip
    host.run(["apparmor_parser", "-r", str(layout.APPARMOR_PROFILE)])


def _clone(ctx: Install, source: Path, dest: Path, origin: str, owner: str) -> None:
    host = ctx.host
    if host.exists(dest):
        host.say(f"  {dest} is already there; kept as it is")
        return
    host.run(["git", "-c", f"safe.directory={source}", "clone", "--no-hardlinks", str(source), str(dest)])
    host.run(["git", "-C", str(dest), "remote", "set-url", "origin", origin])
    host.run(["chown", "-R", f"{owner}:{owner}", str(dest)])


def agent_checkout(ctx: Install) -> None:
    """The agent user's own checkout of the product; the owner keeps theirs."""
    _clone(ctx, ctx.product, ctx.checkout(), ctx.product_origin, AGENT_USER)


def warden_clone(ctx: Install) -> None:
    """The warden's own clone of the factory, where each settings save is committed and pushed (plan R9), on the
    factory's default branch with its upstream set, as the owner's git identity."""
    host, owner = ctx.host, ctx.owner.name
    clone = Path(ctx.need_settings().get_str("factory.clone"))
    origin_head = ["git", "-C", str(ctx.factory), "symbolic-ref", "--short", "refs/remotes/origin/HEAD"]
    head = host.query(origin_head, user=owner)
    branch = head[1].strip().removeprefix("origin/")
    if (head[0] != 0 or not branch) and not host.dry:
        raise refuse(f"{ctx.factory} names no default branch: run `git remote set-head origin --auto` there")
    branch = branch or "<default branch>"
    fresh = not host.exists(clone)
    _clone(ctx, ctx.factory, clone, ctx.factory_origin, WARDEN_USER)
    if fresh:
        host.run(["git", "-C", str(clone), "checkout", "-q", "-B", branch, "--track", f"origin/{branch}"],
                 user=WARDEN_USER)  # fmt: skip
    for key in ("user.name", "user.email"):
        value = host.query(["git", "-C", str(ctx.factory), "config", key], user=owner)[1].strip()
        if not value and not host.dry:
            raise refuse(f"set your git {key} in {ctx.factory}: the warden commits settings saves as you")
        host.run(["git", "-C", str(clone), "config", key, value or f"<{key}>"], user=WARDEN_USER)
    _known_hosts(ctx)


def _known_hosts(ctx: Install) -> None:
    """github.com's host keys for the warden's pushes, taken from the owner's own known_hosts when it has them."""
    host = ctx.host
    mine = ctx.owner.home / ".ssh" / "known_hosts"
    lines = host.query(["ssh-keygen", "-F", "github.com", "-f", str(mine)], user=ctx.owner.name)[1]
    keys = [line for line in lines.splitlines() if line.strip() and not line.startswith("#")]
    if not keys:
        keys = [line for line in host.query(["ssh-keyscan", "-t", "ed25519,ecdsa,rsa", "github.com"])[1].splitlines()
                if line.strip() and not line.startswith("#")]  # fmt: skip
        host.say("  github.com's host keys came from ssh-keyscan: compare them with GitHub's published ones")
    if not keys and not host.dry:
        raise refuse("github.com's host keys could not be found: add them to your ~/.ssh/known_hosts, then reinstall")
    ssh = ctx.places.state / ".ssh"
    host.mkdir(ssh, mode=0o700, owner=WARDEN_USER)
    host.write(ssh / "known_hosts", "\n".join(keys) + "\n", mode=0o644, owner=WARDEN_USER)


def rootless_docker(ctx: Install) -> None:
    """The stack user's own Docker daemon: a container's root is that user on the host, never root."""
    host, stack = ctx.host, layout.STACK_USER
    if host.query(["grep", "-q", f"^{stack}:", "/etc/subuid"])[0] != 0 and not host.dry:
        raise refuse(f"{stack} has no subordinate ids in /etc/subuid: run `usermod --add-subuids` for it")
    if host.query(["which", layout.ROOTLESS_SETUP])[0] != 0 and not host.dry:
        raise refuse(f"{layout.ROOTLESS_SETUP} is missing: install docker-ce-rootless-extras, then install again")
    host.run(["systemctl", "start", f"user@{ctx.uid(stack)}.service"])
    if host.exists(layout.STACK_HOME / ".config" / "systemd" / "user" / "docker.service"):
        host.say("  rootless Docker is already set up")
    else:
        host.run([layout.ROOTLESS_SETUP, "install"], user=stack)
    host.run(["systemctl", "--user", "enable", "--now", "docker.service"], user=stack)


def _public_half(key: Path) -> Path:
    return key.with_name(key.name + ".pub")  # where ssh-keygen -f <key> writes it


def secrets(ctx: Install) -> None:
    """The 6-digit code's secret (shown once), the deploy key, the product CI's read key for the factory repo, the
    owner's GitHub token, the agents' Claude account, and the server's keys the server set-up saved for the owner."""
    host, folder = ctx.host, layout.secrets(ctx.places.state)
    github = ctx.need_settings().get_str("repo.github")
    if not host.exists(ctx.places.totp_secret):
        secret = totp.new_secret()
        host.write(ctx.places.totp_secret, secret + "\n", mode=0o600, owner=WARDEN_USER)
        if host.dry:
            host.show_code("<the secret>", "<the authenticator link>")
        else:
            host.show_code(secret, totp.enrolment_uri(secret, ctx.owner.name))
    # With the owner's signed-in gh the install puts every GitHub key in place itself; without it, each is shown to add
    # by hand.
    gh = "" if host.dry else github_keys.owner_gh(ctx)
    if not gh and not host.dry:
        host.say("  your `gh` is not signed in to github.com: the keys below are yours to add by hand")
    key = folder / "deploy.key"
    if not host.exists(key):
        host.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", DEPLOY_TITLE, "-f", str(key)], user=WARDEN_USER)
        if not gh:
            public = host.read(_public_half(key)).strip() if not host.dry else "<the public key>"
            why = "The warden pushes the agents' work with it."
            host.show_key(DeployKey(github, DEPLOY_TITLE, public, True, why, "", "", ""))
    # Kept in step on every install, so a key the owner never added, or one removed by hand, comes back.
    if gh:
        said = github_keys.put_deploy_key(ctx, gh, github, github_keys.WARDEN_TITLE, host.read(_public_half(key)),
                                          write=True)  # fmt: skip
        host.say(f"  the warden's deploy key on {github}: {said}")
    # Only the product's CI uses the private half, so the machine keeps just the public half, the mark it was made.
    read_key = folder / "factory-read.key"
    if not host.exists(_public_half(read_key)):
        host.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", READ_TITLE, "-f", str(read_key)],
                 user=WARDEN_USER)  # fmt: skip
        factory = github_name(ctx.factory_origin) or ctx.factory_origin
        if gh:
            title = github_keys.read_title(github)
            said = github_keys.put_deploy_key(ctx, gh, factory, title, host.read(_public_half(read_key)), write=False)
            github_keys.put_secret(ctx, gh, github, github_keys.READ_SECRET, read_key)
            host.say(f"  the CI's read key on {factory}: {said}; its private half is {github}'s secret "
                     f"{github_keys.READ_SECRET}")  # fmt: skip
        else:
            public = host.read(_public_half(read_key)).strip() if not host.dry else "<the public key>"
            private = host.read(read_key).strip() if not host.dry else "<the private key>"
            why = f"{github}'s CI checks out the factory with it."
            host.show_key(DeployKey(factory, READ_TITLE, public, False, why, private, READ_SECRET, github))
        host.remove(read_key)
    if not host.exists(folder / "github.token"):
        value = host.ask(
            "Your GitHub token (Actions: write) for the warden: ", secret=True,
            hint=f"A fine-grained token with Actions: Read and write on {github}; the warden starts its workflows.",
            link="https://github.com/settings/personal-access-tokens/new",
        )  # fmt: skip
        if not value:
            raise refuse("github.token is needed: run the install again and paste it")
        host.write(folder / "github.token", value + "\n", mode=0o600, owner=WARDEN_USER)
    _claude_account(ctx, folder)
    _server_keys(ctx, folder)


def _claude_account(ctx: Install, folder: Path) -> None:
    """The agents' first Claude account, asked for only when none exists. A lone claude.token, from an install before
    accounts, becomes the account named main. The owner adds and switches the rest with `factory account`."""
    host, accounts = ctx.host, claude_accounts.folder(folder)
    if host.exists(accounts / claude_accounts.ACTIVE):
        host.say("  the agents' Claude accounts are kept: `factory account list` shows them")
        return
    lone = folder / claude_accounts.UNIT_TOKEN
    if host.exists(lone):
        name, token = claude_accounts.DEFAULT, host.read(lone) if not host.dry else "<the token>"
        host.say(f"  the agents' token becomes the Claude account {name}")
    else:
        token = host.ask(
            "The token `claude setup-token` printed, for the agents' sessions: ", secret=True,
            hint="Run `claude setup-token` in a terminal, sign in with the account the agents should use, and paste "
                 "the token it prints.",
            link="https://code.claude.com/docs/en/authentication#generate-a-long-lived-token",
        )  # fmt: skip
        if not token:
            raise refuse("the agents' Claude token is needed: run the install again and paste it")
        asked = f"A name for this Claude account (Enter for {claude_accounts.DEFAULT}): "
        hint = f"Small letters, digits and dashes, to tell your accounts apart; empty for {claude_accounts.DEFAULT}."
        name = host.ask(asked, secret=False, hint=hint, link="") or claude_accounts.DEFAULT
    if not host.dry:
        try:
            name, token = claude_accounts.check_name(name), claude_accounts.check_token(token)
        except AccountError as exc:
            raise refuse(f"the agents' Claude account cannot be kept: {exc}") from exc
    host.mkdir(accounts, mode=0o700, owner=WARDEN_USER)
    host.write(claude_accounts.token_file(folder, name), token + "\n", mode=0o600, owner=WARDEN_USER)
    host.write(lone, token + "\n", mode=0o600, owner=WARDEN_USER)
    host.write(accounts / claude_accounts.ACTIVE, name + "\n", mode=0o600, owner=WARDEN_USER)


def _server_keys(ctx: Install, folder: Path) -> None:
    """The server's GlitchTip and Langfuse keys, for the warden alone: it imports prod's errors with them."""
    host, theirs = ctx.host, ctx.owner.home / layout.OWNER_SERVER_KEYS
    if not host.exists(theirs):
        host.say(f"  {theirs} is missing: the server set-up has not run, so the warden reads no server errors yet")
        return
    text = host.read(theirs)
    try:
        keys = json.loads(text)
    except ValueError as exc:
        raise refuse(f"{theirs} is not JSON ({exc}): run the server set-up again") from exc
    if not isinstance(keys, dict):
        raise refuse(f"{theirs} is not a JSON object: run the server set-up again")
    host.write(folder / layout.SERVER_KEYS, text, mode=0o600, owner=WARDEN_USER)
