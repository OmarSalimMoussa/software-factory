"""The server set-up kit, run on the owner's laptop from a factory checkout, against the product's one server.

    python3 -m standard.server.setup --product <checkout> [--fresh] [--dry-run] [--ci-key <file.pub>]
                                     [--healthchecks-ping <url>]
    python3 -m standard.server.setup --product <checkout> --lockdown

It signs in as [server] admin over SSH, trusting only [server] host_key, and uses `sudo -n`. Each step prints what
it found and what it changed; a second run changes nothing. docs/server.md says what each step is for.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import shlex
import sys
import tarfile
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from standard.config import FACTORY_REF, FACTORY_REF_LINE, FILE, ConfigError, load
from standard.config_records import Config
from standard.server import heartbeat as heartbeat_mod
from standard.server import setup_machine as machine
from standard.server.setup_machine import FACTORY, Host, SetupError, ensure_dirs, ensure_file, remote_text
from standard.services import provision, stack
from standard.services.provision import Run

CODE = "/usr/local/lib/software-factory"
PRODUCT_TOML = "/etc/software-factory/product.toml"
SUDOERS = "/etc/sudoers.d/factory-ci"
CRON = "/etc/cron.d/factory-gc"
HEARTBEAT_CRON = "/etc/cron.d/factory-heartbeat"
SERVICES = Path("/srv/factory/services")
CI_USER = "ci"
GATE = 'command="/usr/local/bin/factory-ci-gate",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding'
COMMANDS = {
    "factory-deploy": "standard.server.deploy",
    "factory-ci-gate": "standard.server.ci_gate",
    "factory-gc": "standard.server.gc",
    "factory-heartbeat": "standard.server.heartbeat",
}
SERVE_HTTPS = tuple(stack.PORTS[name] for name in ("glitchtip", "langfuse", "minio", "pgweb"))
SERVE_TCP = (stack.PORTS["postgres"],)

# -I keeps the caller's environment and current folder off the import path, so the path is set here instead.
WRAPPER = """#!/usr/bin/python3 -I
import runpy
import sys

sys.path.insert(0, "{code}")
runpy.run_module("{module}", run_name="__main__", alter_sys=True)
"""
_MANIFEST = 'cd "$1" 2>/dev/null || exit 0; find . -type f -print0 | xargs -0 -r sha256sum'
_INSTALL_CODE = (
    'set -e; rm -rf "$1.new" "$1.old"; mkdir -m 755 "$1.new"; base64 -d | tar -x --no-same-owner -C "$1.new"; '
    'if [ -e "$1" ]; then mv "$1" "$1.old"; fi; mv "$1.new" "$1"; rm -rf "$1.old"'
)
_PING = re.compile(r"^https://[A-Za-z0-9.-]+/[A-Za-z0-9/_-]+$")
_KEY = re.compile(r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)) [A-Za-z0-9+/=]+( [^\n]*)?$")


@dataclass
class Job:
    config: Config
    product_toml: str
    host: Host
    local: Run
    fresh: bool
    ci_key: Path | None  # None: a pair made in ``home``
    home: Path  # the owner's ~/.config/software-factory
    factory: Path
    factory_ref: str  # the factory commit the product's CI plans every build with
    code: dict[str, bytes] = field(default_factory=dict)  # path under CODE -> content
    admin_group: str = ""
    services: dict[str, str] = field(default_factory=dict)
    token: str = ""
    todo: list[str] = field(default_factory=list)
    ping: str = ""  # prod's Healthchecks.io ping address; "" keeps the one the server holds

    @property
    def admin(self) -> str:
        return f"{self.config.server.admin}:{self.admin_group}"


def _local(job: Job, argv: list[str], what: str) -> str:
    done = job.local(argv, None)
    if done.returncode != 0:
        raise SetupError([f"{what}: {(done.stderr or done.stdout).strip()}"])
    return done.stdout


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
    dirty = _local(job, [*git, "status", "--porcelain"], "reading the factory checkout").strip()
    if dirty:
        raise SetupError(
            ["the factory checkout has changes that are not committed; the server runs only a commit: " + dirty]
        )
    head = _local(job, [*git, "rev-parse", "HEAD"], "reading the factory's commit").strip()
    if head != job.factory_ref:
        raise SetupError([f"the factory checkout is at {head[:8]}, and the product's {FACTORY_REF} names "
                          f"{job.factory_ref[:8]}: check out that commit, so the server runs the factory the product's "
                          "CI plans its builds with"])  # fmt: skip
    files = _local(job, [*git, "ls-files", "-z", "--", "standard"], "listing the factory's code").split("\0")
    job.code = {"COMMIT": f"{head}\n".encode()}
    job.code |= {name: (job.factory / name).read_bytes() for name in files if name}
    print(f"Factory commit {head[:8]}, {len(job.code) - 1} files of standard/.")


def users(job: Job) -> None:
    host, admin = job.host, job.config.server.admin
    job.admin_group = host.must(["id", "-gn", admin], "reading the admin user's group").strip()
    if "docker" in host.must(["id", "-nG", admin], "reading the admin user's groups").split():
        print(f"   found: {admin} is in the docker group")
    else:
        host.change(f"add {admin} to the docker group", ["sudo", "-n", "usermod", "-aG", "docker", admin])
    found = host.read(["getent", "passwd", CI_USER])
    if found.returncode == 0:
        home = found.stdout.strip().split(":")[5]
        group = host.must(["id", "-gn", CI_USER], "reading the ci user's group").strip()
        print(f"   found: the {CI_USER} user, home {home}")
    else:
        home, group = f"/home/{CI_USER}", CI_USER
        host.change(
            f"create the {CI_USER} user", ["sudo", "-n", "useradd", "-m", "-d", home, "-s", "/bin/bash", CI_USER]
        )
    key = _ci_key(job)
    ensure_dirs(host, [(f"{home}/.ssh", f"{CI_USER}:{group}", "700")])
    ensure_file(host, f"{home}/.ssh/authorized_keys", f"{GATE} {key}\n", owner=f"{CI_USER}:{group}", mode="600")


def _ci_key(job: Job) -> str:
    """The public half of the key CI deploys with: the owner's, or a pair kept on this laptop."""
    if job.ci_key is not None:
        text = job.ci_key.read_text().strip()
        if not _KEY.fullmatch(text):
            raise SetupError([f"{job.ci_key} is not one SSH public key line (type, key, comment)"])
        return text
    private = job.home / "ci_ed25519"
    public = private.with_name("ci_ed25519.pub")
    if public.exists():
        print(f"   found: the CI key pair {private}")
    elif job.host.dry_run:
        print(f"   would make the CI key pair {private}")
        return "(the new key)"
    else:
        job.home.mkdir(mode=0o700, parents=True, exist_ok=True)
        comment = f"factory-ci@{job.config.product.name}"
        _local(job, ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(private)], "ssh-keygen")
        print(f"   changed: made the CI key pair {private}")
    return public.read_text().strip()


def _tarball(files: dict[str, bytes]) -> str:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        folders = sorted({str(p) for name in files for p in Path(name).parents if str(p) != "."})
        for name in folders:
            info = tarfile.TarInfo(name)
            info.type, info.mode = tarfile.DIRTYPE, 0o755
            tar.addfile(info)
        for name, content in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(content), 0o644
            tar.addfile(info, io.BytesIO(content))
    return base64.b64encode(buffer.getvalue()).decode()


def code(job: Job) -> None:
    host, admin = job.host, job.config.server.admin
    want = {f"./{name}": hashlib.sha256(content).hexdigest() for name, content in job.code.items()}
    listed = host.read(["sudo", "-n", "sh", "-c", _MANIFEST, "manifest", CODE]).stdout
    found = {path: digest for digest, _, path in (line.partition("  ") for line in listed.splitlines())}
    commit = job.code["COMMIT"].decode().strip()[:8]
    if found == want:
        print(f"   found: {CODE} holds commit {commit}")
    else:
        what = f"install the factory's code at commit {commit} in {CODE}"
        host.change(what, ["sudo", "-n", "sh", "-c", _INSTALL_CODE, "install", CODE], _tarball(job.code))
    for name in ("factory-deploy", "factory-ci-gate"):
        ensure_file(host, f"/usr/local/bin/{name}", _wrapper(name), owner="root:root", mode="755")
    ensure_dirs(host, [(str(Path(PRODUCT_TOML).parent), "root:root", "755")])
    ensure_file(host, PRODUCT_TOML, job.product_toml, owner="root:root", mode="644")
    sudoers = f"{CI_USER} ALL=({admin}) NOPASSWD: /usr/local/bin/factory-deploy *\n"
    ensure_file(host, SUDOERS, sudoers, owner="root:root", mode="440", check="visudo -cq -f")


def _wrapper(name: str) -> str:
    return WRAPPER.format(code=CODE, module=COMMANDS[name])


def folders(job: Job) -> None:
    owner, name = job.admin, job.config.product.name
    wanted = [(path, owner, "755") for path in ("/srv/factory", "/srv/factory/services", "/srv/factory/edge")]
    wanted += [(path, owner, "755") for path in ("/srv/factory/edge/sites", "/srv/factory/state", f"/srv/{name}")]
    for ws in job.config.workspaces:
        base = f"/srv/{name}/{ws.name}"
        wanted += [(base, owner, "755"), (f"{base}/stack", owner, "755"), (f"{base}/env", owner, "700")]
        wanted.append((f"{base}/state", owner, "755"))
    ensure_dirs(job.host, wanted)


def shared_services(job: Job) -> None:
    host, owner = job.host, job.admin
    changed = ensure_file(host, f"{SERVICES}/compose.yml", stack.COMPOSE.read_text(), owner=owner, mode="644")
    # The edge holds this file through a bind mount, which keeps the inode it started with: write it in place.
    caddyfile = stack.EDGE_CADDYFILE.read_text()
    edge = ensure_file(host, f"{SERVICES}/edge.Caddyfile", caddyfile, owner=owner, mode="644", in_place=True)
    env_path = f"{SERVICES}/{stack.ENV_FILE}"
    found = remote_text(host, env_path)
    existing = stack.parse_env(found[1], env_path) if found else {}
    job.services = stack.generate_env(job.config, kind="server", existing=existing)
    changed |= ensure_file(host, env_path, stack.render_env(job.services), owner=owner, mode="600")
    bad = stack.unhealthy(host.run, SERVICES, edge=True)
    if bad:
        print("   found: not up and healthy: " + ", ".join(bad))
    if bad or changed:
        host.act("bring the shared services up and wait until healthy", lambda: stack.up(host.run, SERVICES, edge=True))
    else:
        print("   found: every shared service up and healthy")
    if edge:
        reload = ["docker", "exec", "factory-edge", "caddy", "reload", "--config", "/etc/caddy/Caddyfile"]
        host.change("reload the edge's settings", reload)
    _reconnect(job)
    if host.dry_run:
        print("   would make sure of GlitchTip's organization, owner, read-only token and factory project")
        return
    job.token = provision.bootstrap(job.config, job.services, host.run)["token"]
    print("   found: GlitchTip's organization, owner, read-only token and factory project in place")


def _reconnect(job: Job) -> None:
    """A recreated shared container leaves the workspaces' networks; join them again before the next deploy would."""
    for ws in job.config.workspaces:
        nets = (provision.network(job.config, ws.name), provision.SHARED)
        for network, shared in (nets, (provision.telemetry_network(job.config, ws.name), provision.TELEMETRY)):
            listing = ["docker", "network", "inspect", "--format", "{{range .Containers}}{{.Name}} {{end}}", network]
            found = job.host.read(listing)
            if found.returncode != 0:
                print(f"   found: no network {network} yet (its first deploy makes it)")
                continue
            for container in shared:
                if container not in found.stdout.split():
                    join = ["docker", "network", "connect", network, container]
                    job.host.change(f"join {container} to {network}", join)


def clean_up(job: Job) -> None:
    ensure_file(job.host, "/usr/local/bin/factory-gc", _wrapper("factory-gc"), owner="root:root", mode="755")
    line = f"17 4 * * 0 {job.config.server.admin} /usr/local/bin/factory-gc\n"
    text = "# The factory's weekly clean-up of old build cache and dangling images; it logs to /srv/factory/state.\n"
    ensure_file(job.host, CRON, text + line, owner="root:root", mode="644")


def heartbeat(job: Job) -> None:
    host, ping_file = job.host, str(heartbeat_mod.PING_FILE)
    ensure_file(host, "/usr/local/bin/factory-heartbeat", _wrapper("factory-heartbeat"), owner="root:root", mode="755")
    if job.ping:
        ensure_file(host, ping_file, job.ping + "\n", owner=f"root:{job.admin_group}", mode="640")
    elif remote_text(host, ping_file) is None:
        job.todo.append("give prod's Healthchecks.io ping address: run the set-up again with --healthchecks-ping <url>")
        return
    line = f"*/5 * * * * {job.config.server.admin} /usr/local/bin/factory-heartbeat\n"
    text = "# Prod's heartbeat: prod answers as prod, then Healthchecks.io hears from the server.\n"
    ensure_file(host, HEARTBEAT_CRON, text + line, owner="root:root", mode="644")


def tailnet(job: Job) -> None:
    host, name = job.host, job.config.server.tailnet
    done = host.read(["sudo", "-n", "tailscale", "serve", "status", "--json"])
    served = json.loads(done.stdout) if done.returncode == 0 and done.stdout.strip().startswith("{") else {}
    ports, web = served.get("TCP") or {}, served.get("Web") or {}
    missing: list[tuple[str, list[str]]] = []
    for port in SERVE_HTTPS:
        target = f"http://127.0.0.1:{port}"
        handler = ((web.get(f"{name}:{port}") or {}).get("Handlers") or {}).get("/") or {}
        if not ((ports.get(str(port)) or {}).get("HTTPS") is True and handler.get("Proxy") == target):
            missing.append((f"https {port}", ["sudo", "tailscale", "serve", "--bg", f"--https={port}", target]))
    for port in SERVE_TCP:
        if (ports.get(str(port)) or {}).get("TCPForward") != f"127.0.0.1:{port}":
            target = f"tcp://127.0.0.1:{port}"
            missing.append((f"tcp {port}", ["sudo", "tailscale", "serve", "--bg", f"--tcp={port}", target]))
    if not missing:
        print(f"   found: {name} serves " + ", ".join(str(p) for p in SERVE_HTTPS + SERVE_TCP))
        return
    print("   found: not served on the tailnet: " + ", ".join(what for what, _ in missing))
    if job.fresh:
        for what, argv in missing:
            host.change(f"serve {what} on the tailnet", ["sudo", "-n", *argv[1:]])
        return
    job.todo.append(
        "serve the shared services on the tailnet, on the server: " + "; ".join(shlex.join(a) for _, a in missing)
    )


def laptop(job: Job) -> None:
    config, path = job.config, job.home / "server.json"
    data = {
        "glitchtip": {
            "url": stack.public_url(config, "server", stack.PORTS["glitchtip"]),
            "org": config.product.name,
            "token": job.token,
        },
        "langfuse": {
            "url": stack.public_url(config, "server", stack.PORTS["langfuse"]),
            "public_key": job.services["LANGFUSE_INIT_PROJECT_PUBLIC_KEY"],
            "secret_key": job.services["LANGFUSE_INIT_PROJECT_SECRET_KEY"],
        },
    }
    text = json.dumps(data, indent=2) + "\n"
    if path.exists() and path.read_text() == text and path.stat().st_mode & 0o777 == 0o600:
        print(f"   found: {path} up to date")
        return
    job.host.act(f"write {path} (0600) for the warden", lambda: _write_private(path, text))


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w") as out:
        out.write(text)
    os.replace(temp, path)


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
    env = f"{SERVICES}/{stack.ENV_FILE}"
    print()
    if job.host.dry_run:
        print(f"Dry run: nothing was changed; a real run would make {len(changes)} changes.")
    else:
        print("Changed nothing: the server already matched." if not changes else f"Made {len(changes)} changes.")
    print(f"GlitchTip  {stack.public_url(config, 'server', stack.PORTS['glitchtip'])}")
    print(f"Langfuse   {stack.public_url(config, 'server', stack.PORTS['langfuse'])}")
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
