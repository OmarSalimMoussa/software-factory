"""The server set-up kit, run on the owner's laptop from a factory checkout, against the product's one server.

    python3 -m standard.server.setup --product <checkout> [--fresh] [--dry-run] [--healthchecks-ping <url>]
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

from standard.config import FILE, ConfigError, load
from standard.server import setup_machine as machine
from standard.server.setup_machine import FACTORY, Host, SetupError
from standard.server.setup_steps import (
    Job,
    clean_up,
    folders,
    heartbeat,
    laptop,
    on_laptop,
    product_settings,
    shared_services,
    tailnet,
    users,
)
from standard.services import host_paths, provision, stack

_PING = re.compile(r"^https://[A-Za-z0-9.-]+/[A-Za-z0-9/_-]+$")


def prepare(job: Job) -> None:
    """Everything read on the laptop before the server is touched: the factory checkout must be a commit."""
    git = ["git", "-C", str(job.factory)]
    dirty = on_laptop(job, [*git, "status", "--porcelain"], "reading the factory checkout").strip()
    if dirty:
        raise SetupError(
            ["the factory checkout has changes that are not committed; the server runs only a commit: " + dirty]
        )
    head = on_laptop(job, [*git, "rev-parse", "HEAD"], "reading the factory's commit").strip()
    print(f"Factory commit {head[:8]}.")


STEPS: tuple[tuple[str, Callable[[Job], None]], ...] = (
    ("The machine", lambda job: job.todo.extend(machine.check(job.host, job.config, fresh=job.fresh))),
    ("The admin user", users),
    ("The product's settings", product_settings),
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
    print(f"  Sign in as {config.services.owner_email}. The password is GLITCHTIP_OWNER_PASSWORD in {env} on the")
    print(f"  server (readable by {config.server.admin}).")
    for item in job.todo:
        print(f"Still to do: {item}")
    print("Next: install the factory on the server; its Push to prod builds prod there and deploys it.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m standard.server.setup", description=__doc__.splitlines()[0])
    parser.add_argument("--product", type=Path, required=True, help="the product's checkout, holding factory.toml")
    parser.add_argument("--fresh", action="store_true", help="also set the machine up: only for a new server")
    parser.add_argument("--dry-run", action="store_true", help="read everything, change nothing")
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
            job = Job(config, product_toml, host, provision.real_run, args.fresh, home, FACTORY)
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
