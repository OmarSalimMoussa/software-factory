from __future__ import annotations

import base64
import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import pytest
from test_stack import CONFIG, FACTORY_TOML

from standard.server import heartbeat, setup
from standard.server import setup_machine as machine
from standard.server.setup_machine import Host, SetupError
from standard.services import provision, stack

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOwner ci@laptop"
UFW_READY = "Status: active\n\nTo  Action  From\n--  ------  ----\n" + "".join(
    f"{port}/tcp{v6}  ALLOW  Anywhere{v6}\n" for v6 in ("", " (v6)") for port in (22, 80, 443)
)
READY: dict[str, tuple[Any, ...]] = {
    "ufw": (0, UFW_READY),
    "sshd": (0, "port 22\npasswordauthentication no\nkbdinteractiveauthentication no\npermitrootlogin no\n"),
    "tailscale": (0, json.dumps({"BackendState": "Running", "Self": {"DNSName": "shop-box.tail0.ts.net."}})),
    "swap": (0, "/swapfile file 4G 0B -2\n"),
    "docker": (0, "29.0.1\n2.40.0\n"),
}
CHECKS = {"ufw": machine.UFW, "sshd": machine.SSHD, "tailscale": machine.TAILSCALE, "swap": machine.SWAP}
CHECKS["docker"] = machine.DOCKER


def _cp(argv: list[str], code: int = 0, out: str = "", err: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, code, out, err)


class Server:
    """A server as the set-up's commands see it: it answers each command the set-up sends and nothing else."""

    def __init__(self, **checks: tuple[Any, ...]) -> None:
        self.answers: dict[tuple[str, ...], tuple[Any, ...]] = {tuple(CHECKS[name]): READY[name] for name in READY}
        self.answers |= {tuple(CHECKS[name]): answer for name, answer in checks.items()}
        self.files: dict[str, tuple[str, str]] = {}
        self.dirs: dict[str, str] = {}
        self.code: dict[str, str] = {}
        self.networks: dict[str, list[str]] = {}
        self.groups = ["deploy", "sudo"]
        self.ci = False
        self.ups = 0
        self.calls: list[list[str]] = []
        ports = {str(p): {"HTTPS": True} for p in setup.SERVE_HTTPS} | {"5434": {"TCPForward": "127.0.0.1:5434"}}
        web = {f"shop-box.tail0.ts.net:{p}": {"Handlers": {"/": {"Proxy": f"http://127.0.0.1:{p}"}}} for p in ports}
        self.served = {"TCP": ports, "Web": web}

    def __call__(self, argv: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if tuple(argv) in self.answers:
            return _cp(argv, *self.answers[tuple(argv)])
        if argv[:4] == ["sudo", "-n", "sh", "-c"]:
            return self.script(argv, argv[4], argv[6:], stdin)
        if argv[:2] == ["docker", "compose"]:
            return self.compose(argv)
        found = self.users(argv)
        if found is not None:
            return found
        if argv[:3] == ["docker", "network", "inspect"]:
            joined = self.networks.get(argv[-1])
            return _cp(argv, 1, "", "no such network") if joined is None else _cp(argv, 0, " ".join(joined) + " \n")
        if argv[:3] == ["docker", "network", "connect"]:
            self.networks[argv[3]].append(argv[4])
            return _cp(argv)
        if argv[:3] == ["docker", "exec", "factory-edge"] or argv[:5] == ["sudo", "-n", "tailscale", "serve", "--bg"]:
            return _cp(argv)
        if argv == ["sudo", "-n", "tailscale", "serve", "status", "--json"]:
            return _cp(argv, 0, json.dumps(self.served))
        raise AssertionError(f"the set-up sent a command the server does not know: {argv}")

    def script(
        self, argv: list[str], script: str, args: list[str], stdin: str | None
    ) -> subprocess.CompletedProcess[str]:
        if script == machine.READ_FILE:
            found = self.files.get(args[0])
            return _cp(argv, 1, "", "No such file") if found is None else _cp(argv, 0, f"{found[0]}\n{found[1]}")
        if script in (machine.WRITE_FILE, machine.WRITE_IN_PLACE):
            assert stdin is not None
            self.files[args[0]] = (f"{args[1]} {args[2]}", stdin)
            return _cp(argv)
        if script == machine.READ_DIRS:
            return _cp(argv, 0, "".join(f"{p} {self.dirs.get(p, 'missing')}\n" for p in args))
        if script == machine.MAKE_DIR:
            self.dirs[args[0]] = f"{args[1]} {args[2]}"
            return _cp(argv)
        if script == setup._MANIFEST:
            return _cp(argv, 0, "".join(f"{digest}  {path}\n" for path, digest in self.code.items()))
        if script == setup._INSTALL_CODE:
            assert stdin is not None
            with tarfile.open(fileobj=io.BytesIO(base64.b64decode(stdin))) as tar:
                files = [(m.name, tar.extractfile(m)) for m in tar.getmembers() if m.isfile()]
                self.code = {f"./{name}": hashlib.sha256(f.read()).hexdigest() for name, f in files if f}
            return _cp(argv)
        raise AssertionError(f"unknown script: {argv}")

    def compose(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        if "up" in argv:
            self.ups += 1
            return _cp(argv)
        rows = [
            {"Service": s, "State": "running", "Health": "healthy", "ExitCode": 0} for s in stack.services(edge=True)
        ]
        rows = [r | {"State": "exited"} if r["Service"] in stack.ONE_SHOT else r for r in rows]
        return _cp(argv, 0, "\n".join(json.dumps(r) for r in rows) if self.ups else "")

    def users(self, argv: list[str]) -> subprocess.CompletedProcess[str] | None:
        if argv == ["id", "-gn", "deploy"] or argv == ["id", "-gn", "ci"]:
            return _cp(argv, 0, f"{argv[2]}\n")
        if argv == ["id", "-nG", "deploy"]:
            return _cp(argv, 0, " ".join(self.groups) + "\n")
        if argv == ["sudo", "-n", "usermod", "-aG", "docker", "deploy"]:
            self.groups.append("docker")
            return _cp(argv)
        if argv == ["getent", "passwd", "ci"]:
            return _cp(argv, 0, "ci:x:1001:1001::/home/ci:/bin/bash\n") if self.ci else _cp(argv, 2)
        if argv[:3] == ["sudo", "-n", "useradd"]:
            self.ci = True
            return _cp(argv)
        return None


@pytest.fixture
def make_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    factory = tmp_path / "factory"
    (factory / "standard").mkdir(parents=True)
    (factory / "standard" / "config.py").write_text("print('the factory')\n")
    key = tmp_path / "ci.pub"
    key.write_text(KEY + "\n")
    bootstrap = {"token": "read-token", "factory_dsn": "http://k@factory-glitchtip:8000/1"}
    monkeypatch.setattr(provision, "bootstrap", lambda config, services, run: bootstrap)

    def make(server: Server, *, dirty: str = "", fresh: bool = False, ref: str = "a" * 40) -> setup.Job:
        def local(argv: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
            answers = {"status": dirty, "rev-parse": "a" * 40 + "\n", "ls-files": "standard/config.py\0"}
            return _cp(argv, 0, answers[argv[3]])

        host = Host(server, dry_run=False)
        return setup.Job(CONFIG, FACTORY_TOML, host, local, fresh, key, tmp_path / "home", factory, ref)

    return make


@pytest.mark.parametrize(
    ("check", "answer", "said"),
    [
        ("ufw", (0, UFW_READY + "8080  ALLOW  Anywhere\n"), "allowing 22, 80, 443, 8080"),
        (
            "sshd",
            (0, "passwordauthentication yes\nkbdinteractiveauthentication no\npermitrootlogin no\n"),
            "--lockdown",
        ),
        ("tailscale", (3, ""), "Tailscale is not installed"),
        ("swap", (0, ""), "swap none"),
        ("docker", (1, "", "sudo: docker: command not found"), "Docker is not ready"),
    ],
)
def test_an_existing_machine_that_fails_a_check_stops_without_changing_anything(
    make_job: Any, check: str, answer: tuple[Any, ...], said: str
) -> None:
    server = Server(**{check: answer})
    job = make_job(server)

    with pytest.raises(SetupError) as raised:
        setup.run(job)

    assert said in " ".join(raised.value.problems)
    assert server.calls == list(CHECKS.values()) and job.host.changes == []


def test_a_second_run_changes_nothing_and_says_so(make_job: Any, capsys: pytest.CaptureFixture[str]) -> None:
    server = Server()
    first = make_job(server)
    setup.run(first)
    capsys.readouterr()

    second = make_job(server)
    setup.run(second)

    assert first.host.changes and second.host.changes == [] and server.ups == 1
    assert "Changed nothing: the server already matched." in capsys.readouterr().out
    saved = json.loads((first.home / "server.json").read_text())
    assert saved["glitchtip"] == {"url": "https://shop-box.tail0.ts.net:8200", "org": "shop", "token": "read-token"}
    assert (first.home / "server.json").stat().st_mode & 0o777 == 0o600


def test_the_ci_key_is_the_one_key_and_can_only_run_the_gate(make_job: Any) -> None:
    server = Server()

    setup.run(make_job(server))

    gate = 'command="/usr/local/bin/factory-ci-gate",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding'
    assert server.files["/home/ci/.ssh/authorized_keys"] == ("ci:ci 600", f"{gate} {KEY}\n")
    assert server.files[setup.SUDOERS] == (
        "root:root 440",
        "ci ALL=(deploy) NOPASSWD: /usr/local/bin/factory-deploy *\n",
    )
    write = next(c for c in server.calls if setup.SUDOERS in c and machine.WRITE_FILE in c)
    assert write[-1] == "visudo -cq -f"


@pytest.mark.parametrize(
    ("job", "said"),
    [
        ({"dirty": " M standard/config.py\n"}, "not committed"),
        (
            {"ref": "b" * 40},
            "the factory checkout is at aaaaaaaa, and the product's .github/factory-ref names bbbbbbbb",
        ),
    ],
)
def test_a_checkout_the_product_does_not_name_is_refused_before_the_server_is_touched(
    make_job: Any, job: dict[str, str], said: str
) -> None:
    server = Server()

    with pytest.raises(SetupError, match=said):
        setup.run(make_job(server, **job))

    assert server.calls == []


def test_a_fresh_machine_allows_ssh_before_the_firewall_goes_on(make_job: Any) -> None:
    server = Server(ufw=(0, "Status: inactive\n"))
    server.answers[("sudo", "-n", "ufw", "--force", "enable")] = (0, "")
    for step in (["update"], ["install", "-y", "ufw"]):
        server.answers[tuple(["sudo", "-n", "env", "DEBIAN_FRONTEND=noninteractive", "apt-get", *step])] = (0, "")
    for port in ("22", "80", "443"):
        server.answers[("sudo", "-n", "ufw", "allow", f"{port}/tcp")] = (0, "")
    job = make_job(server, fresh=True)
    original = server.__call__

    def enable_then_ready(argv: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
        if argv[-2:] == ["--force", "enable"]:
            server.answers[tuple(machine.UFW)] = READY["ufw"]
        return original(argv, stdin)

    job.host.run = enable_then_ready
    setup.run(job)

    ufw = [c[3:] for c in server.calls if c[:3] == ["sudo", "-n", "ufw"] and c[3] != "status"]
    assert ufw == [["allow", "22/tcp"], ["allow", "80/tcp"], ["allow", "443/tcp"], ["--force", "enable"]]


def test_a_rerun_joins_the_shared_containers_back_to_each_workspace_network(make_job: Any) -> None:
    server = Server()
    server.networks = {"shop-prod-services": ["shop-prod-web", "factory-postgres"], "shop-prod-telemetry": []}

    setup.run(make_job(server))

    assert server.networks == {
        "shop-prod-services": ["shop-prod-web", "factory-postgres", "factory-langfuse", "factory-glitchtip"],
        "shop-prod-telemetry": ["factory-langfuse", "factory-glitchtip"],
    }


def test_the_heartbeat_runs_on_the_server_once_it_has_prod_s_ping_address(make_job: Any) -> None:
    server = Server()
    first = make_job(server)
    setup.run(first)
    assert setup.HEARTBEAT_CRON not in server.files
    assert any("--healthchecks-ping" in item for item in first.todo)

    second = make_job(server)
    second.ping = "https://hc-ping.com/5f1c"
    setup.run(second)
    third = make_job(server)  # a later run without the address keeps the one the server holds
    setup.run(third)

    assert server.files[str(heartbeat.PING_FILE)] == ("root:deploy 640", "https://hc-ping.com/5f1c\n")
    assert server.files[setup.HEARTBEAT_CRON][1].endswith("*/5 * * * * deploy /usr/local/bin/factory-heartbeat\n")
    assert third.host.changes == [] and third.todo == []


@pytest.mark.parametrize(
    ("answer", "pinged", "code"),
    [
        (b'{"workspace": "prod", "build_commit": "abcd1234"}', ["https://hc-ping.com/5f1c"], 0),
        (b'{"workspace": "staging"}', ["https://hc-ping.com/5f1c/fail"], 1),
        (OSError("refused"), ["https://hc-ping.com/5f1c/fail"], 1),
    ],
    ids=["prod-answers", "wrong-workspace", "no-answer"],
)
def test_the_heartbeat_checks_in_only_when_prod_answers_as_prod(
    answer: bytes | OSError, pinged: list[str], code: int
) -> None:
    calls: list[str] = []

    def get(url: str, body: bytes | None) -> bytes:
        if url.startswith("https://hc-ping.com/"):
            calls.append(url)
            return b"OK"
        assert url == "https://prod.acme.dev/healthz"
        if isinstance(answer, OSError):
            raise answer
        return answer

    assert heartbeat.beat(CONFIG, "https://hc-ping.com/5f1c", get) == code
    assert calls == pinged
