"""The server set-up kit, run on the owner's laptop from a factory checkout, against the product's one server.

    python3 -m standard.server.setup --product <checkout> [--fresh] [--dry-run] [--ci-key <file.pub>]
                                     [--healthchecks-ping <url>]
    python3 -m standard.server.setup --product <checkout> --lockdown

It signs in as [server] admin over SSH, trusting only [server] host_key, and uses `sudo -n`. Each step prints what
it found and what it changed; a second run changes nothing. docs/server.md says what each step is for.
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from standard.config import FACTORY_REF, FACTORY_REF_LINE, FILE, ConfigError, load
from standard.server import setup_machine as machine
from standard.server.setup_machine import FACTORY, Host, SetupError
from standard.server.setup_steps import (
    Job,
    clean_up,
    code,
    folders,
    heartbeat,
    laptop,
    on_laptop,
    shared_services,
    tailnet,
    users,
)
from standard.services import host_paths, provision, stack

_PING = re.compile(r"^https://[A-Za-z0-9.-]+/[A-Za-z0-9/_-]+$")


def factory_ref(product: Path) -> str:
    """The commit the product's .github/factory-ref names (one line, owner/name@<40-character sha>)."""
    try:
        text = (product / FACTORY_REF).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SetupError([f"{FACTORY_REF} in the product cannot be read: {exc}"]) from exc
    found = FACTORY_REF_LINE.fullmatch(text.strip())
    if found is None:
        raise SetupError([f"{FACTORY_REF} must hold one line, owner/name@<40-character sha>; it holds {text[:80]!r}"])
    return found.group(1)


def prepare(job: Job) -> None:
    """Everything read on the laptop, before the server is touched: the code it will run must be a commit."""
    git = ["git", "-C", str(job.factory)]
    dirty = on_laptop(job, [*git, "status", "--porcelain"], "reading the factory checkout").strip()
    if dirty:
        raise SetupError(
            ["the factory checkout has changes that are not committed; the server runs only a commit: " + dirty]
        )
    head = on_laptop(job, [*git, "rev-parse", "HEAD"], "reading the factory's commit").strip()
    if head != job.factory_ref:
        raise SetupError([f"the factory checkout is at {head[:8]}, and the product's {FACTORY_REF} names "
                          f"{job.factory_ref[:8]}: check out that commit, so the server runs the factory the product's "
                          "CI plans its builds with"])  # fmt: skip
    files = on_laptop(job, [*git, "ls-files", "-z", "--", "standard"], "listing the factory's code").split("\0")
    job.code = {"COMMIT": f"{head}\n".encode()}
    job.code |= {name: (job.factory / name).read_bytes() for name in files if name}
    print(f"Factory commit {head[:8]}, {len(job.code) - 1} files of standard/.")


STEPS: tuple[tuple[str, Callable[[Job], None]], ...] = (
    ("The machine", lambda job: job.todo.extend(machine.check(job.host, job.config, fresh=job.fresh))),
    ("Users and the CI key", users),
    ("The factory's code and commands", code),
    ("Folders", folders),
    ("The shared services", shared_services),
    ("The weekly clean-up", clean_up),
    ("Prod's heartbeat", heartbeat),
    ("The tailnet", tailnet),
    ("The warden's copy on this laptop", laptop),
)


def run(job: Job) -> None:
    prepare(job)
    for number, (title, step) in enumerate(STEPS, 1):
        print(f"{number}. {title}")
        before = len(job.host.changes)
        step(job)
        if len(job.host.changes) == before:
            print("   changed nothing")
    summary(job)


def summary(job: Job) -> None:
    config, changes = job.config, job.host.changes
    env = f"{host_paths.FOLDER}/{stack.ENV_FILE}"
    print()
    if job.host.dry_run:
        print(f"Dry run: nothing was changed; a real run would make {len(changes)} changes.")
    else:
        print("Changed nothing: the server already matched." if not changes else f"Made {len(changes)} changes.")
    print(f"GlitchTip  {stack.public_url(config, stack.PORTS['glitchtip'])}")
    print(f"Langfuse   {stack.public_url(config, stack.PORTS['langfuse'])}")
    print(f"  Sign in to both as {config.services.owner_email}. The passwords are GLITCHTIP_OWNER_PASSWORD and")
    print(f"  LANGFUSE_INIT_USER_PASSWORD in {env} on the server (readable by {config.server.admin}).")
    if job.ci_key is None:
        private = job.home / "ci_ed25519"
        print(f"CI key: save the whole of {private} as the product's GitHub secret SERVER_DEPLOY_KEY.")
    else:
        print(f"CI key: save the private half of {job.ci_key} as the product's GitHub secret SERVER_DEPLOY_KEY.")
    for item in job.todo:
        print(f"Still to do: {item}")
    print("Next: push the product's default branch; its CI checks it, builds it and deploys staging.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m standard.server.setup", description=__doc__.splitlines()[0])
    parser.add_argument("--product", type=Path, required=True, help="the product's checkout, holding factory.toml")
    parser.add_argument("--fresh", action="store_true", help="also set the machine up: only for a new server")
    parser.add_argument("--dry-run", action="store_true", help="read everything, change nothing")
    parser.add_argument("--ci-key", type=Path, help="the public key CI deploys with (default: a pair kept here)")
    parser.add_argument("--lockdown", action="store_true", help="only: sshd takes keys only, and no root")
    parser.add_argument("--healthchecks-ping", default="", help="prod's Healthchecks.io ping address, once")
    args = parser.parse_args(argv)
    if args.healthchecks_ping and not _PING.fullmatch(args.healthchecks_ping):
        print("--healthchecks-ping takes the check's https ping address, such as https://hc-ping.com/<uuid>",
              file=sys.stderr)  # fmt: skip
        return 2
    try:
        config = load(args.product)
        with tempfile.TemporaryDirectory() as folder:
            known = Path(folder) / "known_hosts"
            known.write_text(f"{config.server.host} {config.server.host_key}\n")
            host = Host(machine.ssh_runner(config, known), args.dry_run)
            if args.lockdown:
                machine.lockdown(host, config)
                return 0
            home = Path.home() / ".config" / "software-factory"
            product_toml = (args.product / FILE).read_text()
            ref = factory_ref(args.product)
            job = Job(config, product_toml, host, provision.real_run, args.fresh, args.ci_key, home, FACTORY, ref)
            job.ping = args.healthchecks_ping
            run(job)
    except ConfigError as exc:
        print("factory.toml has problems:\n" + "\n".join(f"  - {p}" for p in exc.problems), file=sys.stderr)
        return 2
    except (SetupError, stack.StackError, provision.ProvisionError) as exc:
        problems = exc.problems if isinstance(exc, SetupError) else [str(exc)]
        print("The set-up stopped:\n" + "\n".join(f"  - {p}" for p in problems), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
