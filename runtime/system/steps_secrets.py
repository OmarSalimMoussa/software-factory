"""The install's secrets: the 6-digit code's secret (shown once), the warden's deploy key, the product CI's read key
for the factory repo, the owner's GitHub token, the agents' Claude account, and the server's keys the server set-up
saved for the owner.

With the owner's signed-in GitHub CLI the install puts every GitHub key in place itself, and asks the owner to sign in
when it is not; only an owner who declines adds the keys by hand. Each piece is made once and kept, and a rerun after
any failure finishes what the last run began: a code's secret is kept only once it was shown, and the read key's
private half stays on this machine only until GitHub holds it.
"""

from __future__ import annotations

import json
from pathlib import Path

from runtime.shared import claude_accounts, releases, totp
from runtime.shared.claude_accounts import AccountError
from runtime.shared.paths import WARDEN_USER
from runtime.system import github_keys, layout
from runtime.system.host import DeployKey
from runtime.system.job import Install, github_name, refuse

# Each key's comment, which GitHub shows as its title.
DEPLOY_TITLE = "software-factory deploy"
READ_TITLE = "software-factory read"
SIGN_IN = (
    "The install puts the factory's keys on GitHub for you, with your GitHub CLI, which is not signed in. In a "
    "terminal, run gh auth login and sign in to github.com, then come back here."
)


def _public_half(key: Path) -> Path:
    return key.with_name(key.name + ".pub")  # where ssh-keygen -f <key> writes it


def _keygen(ctx: Install, key: Path, title: str) -> None:
    """A new key pair at ``key``; any half an earlier run left is removed first, so ssh-keygen never asks."""
    for half in (key, _public_half(key)):
        if ctx.host.exists(half):
            ctx.host.remove(half)
    ctx.host.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", title, "-f", str(key)], user=WARDEN_USER)


def signed_in_gh(ctx: Install) -> str:
    """The owner's GitHub CLI once it is signed in; "" when the owner chose to add the keys by hand."""
    host = ctx.host
    if host.dry:
        return ""
    gh = github_keys.owner_gh(host, ctx.owner)
    while not gh and host.confirm("Sign in to GitHub", SIGN_IN, yes="I signed in", no="I will add the keys myself"):
        gh = github_keys.owner_gh(host, ctx.owner)
    return gh


def secrets(ctx: Install) -> None:
    host, folder = ctx.host, layout.secrets(ctx.places.state)
    github = ctx.need_settings().get_str("repo.github")
    if not host.exists(ctx.places.totp_secret):
        secret = "<the secret>" if host.dry else totp.new_secret()
        # Shown before it is kept: a run stopped while it is on screen makes a new one, never one the owner missed.
        host.show_code(secret, totp.enrolment_uri(secret, ctx.owner.name))
        host.write(ctx.places.totp_secret, secret + "\n", mode=0o600, owner=WARDEN_USER)
    gh = signed_in_gh(ctx)
    key = folder / "deploy.key"
    if not host.exists(key):
        _keygen(ctx, key, DEPLOY_TITLE)
        if not gh:
            public = host.read(_public_half(key)).strip() if not host.dry else "<the public key>"
            why = "The warden pushes the agents' work with it."
            host.show_key(DeployKey(github, DEPLOY_TITLE, public, True, why, "", "", ""))
    # Kept in step on every install, so a key the owner never added, or one removed by hand, comes back.
    if gh:
        said = github_keys.put_deploy_key(ctx, gh, github, github_keys.WARDEN_TITLE, host.read(_public_half(key)),
                                          write=True)  # fmt: skip
        host.say(f"the warden's deploy key on {github}: {said}")
    _read_key(ctx, folder, github, gh)
    if not host.exists(folder / "github.token"):
        value = host.ask(
            "Your GitHub token for the warden: ", secret=True,
            hint=f"A fine-grained token with Actions: Read and write on {github}. The warden starts the product's "
                 "workflows with it.",
            link="https://github.com/settings/personal-access-tokens/new",
        )  # fmt: skip
        if not value:
            raise refuse("The warden needs your GitHub token: install again and paste it when asked")
        host.write(folder / "github.token", value + "\n", mode=0o600, owner=WARDEN_USER)
    _factory_token(ctx)
    _claude_account(ctx, folder)
    _server_keys(ctx, folder)


def _factory_token(ctx: Install) -> None:
    """The owner's read-only token for the factory's own repo, asked for once: the warden finds new releases with it,
    and the dashboard's Upgrade downloads them. Checked on GitHub before it is kept."""
    host, path = ctx.host, releases.token_path(ctx.places)
    if host.exists(path):
        return
    settings = ctx.need_settings()
    factory = settings.get_str("factory.github")
    value = host.ask(
        "A read-only GitHub token for the factory's own repo: ", secret=True,
        hint=f"A fine-grained token with Contents: Read and Actions: Read on {factory}. The dashboard finds and "
             "installs new factory releases with it.",
        link="https://github.com/settings/personal-access-tokens/new",
    )  # fmt: skip
    if not value:
        raise refuse("The factory needs its read-only token to find new releases: install again and paste it")
    if not host.dry:
        try:
            check_factory_token(factory, value, float(settings.get_int("watch.http_timeout_seconds")))
        except releases.ReleaseError as exc:
            raise refuse(f"That token cannot read the factory's releases ({exc}): make one as asked, then install "
                         "again") from exc  # fmt: skip
    host.write(path, value + "\n", mode=0o600, owner=WARDEN_USER)


def check_factory_token(repo: str, token: str, timeout: float) -> None:
    """Raises ReleaseError unless ``token`` reads the factory repo and its CI runs, as an upgrade does."""
    found = releases.Releases(repo, token, timeout, releases.http_get)
    found.default_branch()
    found.can_read_runs()


def _read_key(ctx: Install, folder: Path, github: str, gh: str) -> None:
    """The key the product's CI checks the factory out with. The machine keeps only its public half, the mark it was
    made; a private half still here means an earlier run stopped before GitHub held it, so it is handed over again."""
    host, read_key = ctx.host, folder / "factory-read.key"
    if host.exists(_public_half(read_key)) and not host.exists(read_key):
        return
    if not host.exists(read_key):
        _keygen(ctx, read_key, READ_TITLE)
    factory = github_name(ctx.factory_origin) or ctx.factory_origin
    if gh:
        title = github_keys.read_title(github)
        said = github_keys.put_deploy_key(ctx, gh, factory, title, host.read(_public_half(read_key)), write=False)
        github_keys.put_secret(ctx, gh, github, github_keys.READ_SECRET, read_key)
        host.say(f"the CI's read key on {factory}: {said}; its private half is {github}'s secret "
                 f"{github_keys.READ_SECRET}")  # fmt: skip
    else:
        public = host.read(_public_half(read_key)).strip() if not host.dry else "<the public key>"
        private = host.read(read_key).strip() if not host.dry else "<the private key>"
        why = f"{github}'s CI checks out the factory with it."
        host.show_key(DeployKey(factory, READ_TITLE, public, False, why, private, github_keys.READ_SECRET, github))
    host.remove(read_key)


def _claude_account(ctx: Install, folder: Path) -> None:
    """The agents' first Claude account, asked for only when none exists. The owner adds and switches the rest with
    `factory account`."""
    host, accounts = ctx.host, claude_accounts.folder(folder)
    if host.exists(accounts / claude_accounts.ACTIVE):
        host.say("the agents' Claude accounts are kept")
        return
    lone = folder / claude_accounts.UNIT_TOKEN
    token = host.ask(
        "The token `claude setup-token` printed, for the agents' sessions: ", secret=True,
        hint="Run `claude setup-token` in a terminal, sign in with the account the agents should use, and paste "
             "the token it prints.",
        link="https://code.claude.com/docs/en/authentication#generate-a-long-lived-token",
    )  # fmt: skip
    if not token:
        raise refuse("The agents need a Claude account: install again and paste its token when asked")
    asked = f"A name for this Claude account (Enter for {claude_accounts.DEFAULT}): "
    hint = f"Small letters, digits and dashes, to tell your accounts apart; empty for {claude_accounts.DEFAULT}."
    name = host.ask(asked, secret=False, hint=hint, link="") or claude_accounts.DEFAULT
    if not host.dry:
        try:
            name, token = claude_accounts.check_name(name), claude_accounts.check_token(token)
        except AccountError as exc:
            raise refuse(f"The agents' Claude account cannot be kept ({exc}): install again and paste the token "
                         "and name as asked") from exc  # fmt: skip
    host.mkdir(accounts, mode=0o700, owner=WARDEN_USER)
    host.write(claude_accounts.token_file(folder, name), token + "\n", mode=0o600, owner=WARDEN_USER)
    host.write(lone, token + "\n", mode=0o600, owner=WARDEN_USER)
    host.write(accounts / claude_accounts.ACTIVE, name + "\n", mode=0o600, owner=WARDEN_USER)


def _server_keys(ctx: Install, folder: Path) -> None:
    """The server's GlitchTip and Langfuse keys, for the warden alone: it imports prod's errors with them."""
    host, theirs = ctx.host, ctx.owner.home / layout.OWNER_SERVER_KEYS
    if not host.exists(theirs):
        host.tell("Your server is not set up yet, so the factory reads no errors from it. Set it up, then install "
                  "again to connect them.")  # fmt: skip
        return
    text = host.read(theirs)
    try:
        keys = json.loads(text)
    except ValueError as exc:
        raise refuse(f"The keys your server set-up saved in {theirs} cannot be read: run the server set-up again, "
                     "then install again") from exc  # fmt: skip
    if not isinstance(keys, dict):
        raise refuse(f"The keys your server set-up saved in {theirs} cannot be read: run the server set-up again, "
                     "then install again")  # fmt: skip
    host.write(folder / layout.SERVER_KEYS, text, mode=0o600, owner=WARDEN_USER)
