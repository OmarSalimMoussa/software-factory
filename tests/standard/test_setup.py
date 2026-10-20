from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from test_stack import CONFIG, FACTORY_TOML

from standard.config import ConfigError, parse_text
from standard.server import heartbeat, setup, setup_steps
from standard.server import setup_machine as machine
from standard.server.setup_machine import Host, SetupError
from standard.services import host_paths, provision, stack

UFW_READY = "Status: active\n\nTo  Action  From\n--  ------  ----\n" + "".join(
    f"{port}/tcp{v6}  ALLOW  Anywhere{v6}\n" for v6 in ("", " (v6)") for port in (22, 80, 443)
)
READY: dict[str, tuple[Any, ...]] = {
    "ufw": (0, UFW_READY),
    "sshd": (0, "port 22\npasswordauthentication no\nkbdinteractiveauthentication no\npermitrootlogin no\n"),
    "tailscale": (0, json.dumps({"BackendState": "Running", "Self": {"DNSName": "shop-box.tail0.ts.net."}})),
    "swap": (0, "/swapfile file 4G 0B -2\n"),
    "docker": (0, "29.0.1\n2.40.0\n"),
    "python": (0, "3.12\n"),
}
CHECKS = {"ufw": machine.UFW, "sshd": machine.SSHD, "tailscale": machine.TAILSCALE, "swap": machine.SWAP}
CHECKS["docker"], CHECKS["python"] = machine.DOCKER, machine.PYTHON


def _cp(argv: list[str], code: int = 0, out: str = "", err: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, code, out, err)


class Server:
    """A server as the set-up's commands see it: it answers each command the set-up sends and nothing else."""

    def __init__(self, **checks: tuple[Any, ...]) -> None:
        self.answers: dict[tuple[str, ...], tuple[Any, ...]] = {tuple(CHECKS[name]): READY[name] for name in READY}
        self.answers |= {tuple(CHECKS[name]): answer for name, answer in checks.items()}
        self.files: dict[str, tuple[str, str]] = {}
        self.dirs: dict[str, str] = {}
        self.networks: dict[str, list[str]] = {}
        self.groups = ["deploy", "sudo"]
        self.ups = 0
        self.calls: list[list[str]] = []
        ports = {str(p): {"HTTPS": True} for p in setup_steps.SERVE_HTTPS} | {"5434": {"TCPForward": "127.0.0.1:5434"}}
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
        found = self.users(argv) or self.network(argv) or self.edge(argv)
        if found is not None:
            return found
        raise AssertionError(f"the set-up sent a command the server does not know: {argv}")

    def network(self, argv: list[str]) -> subprocess.CompletedProcess[str] | None:
        if argv[:3] == ["docker", "network", "inspect"]:
            joined = self.networks.get(argv[-1])
            return _cp(argv, 1, "", "no such network") if joined is None else _cp(argv, 0, " ".join(joined) + " \n")
        if argv[:3] == ["docker", "network", "connect"]:
            self.networks[argv[3]].append(argv[4])
            return _cp(argv)
        return None

    def edge(self, argv: list[str]) -> subprocess.CompletedProcess[str] | None:
        if argv[:3] == ["docker", "exec", "factory-edge"] or argv[:5] == ["sudo", "-n", "tailscale", "serve", "--bg"]:
            return _cp(argv)
        if argv == ["sudo", "-n", "tailscale", "serve", "status", "--json"]:
            return _cp(argv, 0, json.dumps(self.served))
        return None

    def script(
        self, argv: list[str], script: str, args: list[str], stdin: str | None
    ) -> subprocess.CompletedProcess[str]:
        if script == machine.READ_FILE:
            found = self.files.get(args[0])
            return _cp(argv, 3) if found is None else _cp(argv, 0, f"{found[0]}\n{found[1]}")
        if script in (machine.WRITE_FILE, machine.WRITE_IN_PLACE):
            assert stdin is not None
            self.files[args[0]] = (f"{args[1]} {args[2]}", stdin)
            return _cp(argv)
        if script == machine.READ_DIRS:
            return _cp(argv, 0, "".join(f"{p} {self.dirs.get(p, 'missing')}\n" for p in args))
        if script == machine.MAKE_DIR:
            self.dirs[args[0]] = f"{args[1]} {args[2]}"
            return _cp(argv)
        raise AssertionError(f"unknown script: {argv}")

    def compose(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        if "up" in argv:
            self.ups += 1
            return _cp(argv)
        rows = [{"Service": s, "State": "running", "Health": "healthy", "ExitCode": 0} for s in stack.services()]
        rows = [r | {"State": "exited"} if r["Service"] in stack.ONE_SHOT else r for r in rows]
        return _cp(argv, 0, "\n".join(json.dumps(r) for r in rows) if self.ups else "")

    def users(self, argv: list[str]) -> subprocess.CompletedProcess[str] | None:
        if argv == ["id", "-gn", "deploy"]:
            return _cp(argv, 0, f"{argv[2]}\n")
        if argv == ["id", "-nG", "deploy"]:
            return _cp(argv, 0, " ".join(self.groups) + "\n")
        if argv == ["sudo", "-n", "usermod", "-aG", "docker", "deploy"]:
            self.groups.append("docker")
            return _cp(argv)
        return None


@pytest.fixture
def make_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    factory = tmp_path / "factory"
    bootstrap = {"token": "read-token"}
    monkeypatch.setattr(provision, "bootstrap", lambda config, services, run: bootstrap)

    def make(server: Server, *, dirty: str = "", fresh: bool = False) -> setup_steps.Job:
        def local(argv: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
            answers = {"status": dirty, "rev-parse": "a" * 40 + "\n"}
            return _cp(argv, 0, answers[argv[3]])

        host = Host(server, dry_run=False)
        return setup_steps.Job(CONFIG, FACTORY_TOML, host, local, fresh, tmp_path / "home", factory)

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
        ("python", (0, "3.10\n"), "Python 3.10. To do: install Python 3.12 or newer"),
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


def test_the_admin_owns_the_product_settings_its_deploys_refresh_and_no_ci_user_key_or_rule_is_placed(
    make_job: Any,
) -> None:
    server = Server()

    setup.run(make_job(server))

    assert server.files[setup_steps.PRODUCT_TOML] == ("deploy:deploy 644", FACTORY_TOML)
    assert sorted(path for path in server.files if path.startswith("/usr/local/")) == [
        "/usr/local/bin/factory-gc",
        "/usr/local/bin/factory-heartbeat",
    ]
    assert 'sys.path.insert(0, "/opt/software-factory")' in server.files["/usr/local/bin/factory-gc"][1]
    assert not [path for path in server.files if path.startswith(("/home/", "/etc/sudoers.d/"))]
    assert not [argv for argv in server.calls if "ci" in argv or "useradd" in argv]


def test_a_factory_checkout_with_uncommitted_changes_is_refused_before_the_server_is_touched(make_job: Any) -> None:
    server = Server()

    with pytest.raises(SetupError, match="not committed"):
        setup.run(make_job(server, dirty=" M standard/config.py\n"))

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
        "shop-prod-services": ["shop-prod-web", "factory-postgres", "factory-glitchtip"],
        "shop-prod-telemetry": ["factory-glitchtip"],
    }


def test_the_heartbeat_runs_on_the_server_once_it_has_prod_s_ping_address(make_job: Any) -> None:
    server = Server()
    first = make_job(server)
    setup.run(first)
    assert setup_steps.HEARTBEAT_CRON not in server.files
    assert any("--healthchecks-ping" in item for item in first.todo)

    second = make_job(server)
    second.ping = "https://hc-ping.com/5f1c"
    setup.run(second)
    third = make_job(server)  # a later run without the address keeps the one the server holds
    setup.run(third)

    assert server.files[str(heartbeat.PING_FILE)] == ("root:deploy 640", "https://hc-ping.com/5f1c\n")
    assert server.files[setup_steps.HEARTBEAT_CRON][1].endswith("*/5 * * * * deploy /usr/local/bin/factory-heartbeat\n")
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


def test_the_server_needs_the_python_the_factorys_pyproject_declares(
    make_job: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nrequires-python = ">=3.99"\n')
    monkeypatch.setattr(machine, "FACTORY", tmp_path, raising=False)

    with pytest.raises(SetupError) as raised:
        setup.run(make_job(Server()))

    assert "Python 3.12. To do: install Python 3.99 or newer" in " ".join(raised.value.problems)


def test_shared_service_secrets_are_kept_never_made_anew_over_a_blank_or_unreadable_file(make_job: Any) -> None:
    held = stack.generate_env(CONFIG, existing={})
    blank = {**held, "POSTGRES_PASSWORD": ""}
    with pytest.raises(stack.StackError, match="POSTGRES_PASSWORD empty"):
        stack.generate_env(CONFIG, existing=blank)
    assert stack.generate_env(CONFIG, existing=held)["POSTGRES_PASSWORD"] == held["POSTGRES_PASSWORD"]

    server = Server()
    env_file = f"{host_paths.FOLDER}/{stack.ENV_FILE}"
    server.answers[("sudo", "-n", "sh", "-c", machine.READ_FILE, "read", env_file)] = (1, "", "cat: Permission denied")
    with pytest.raises(SetupError, match="on the server but cannot be read"):
        setup.run(make_job(server))


REPORTS = '[reports]\nport = 8110\npath = "/api/export"\nheader = "X-Shop-Token"\ntoken_key = "SHOP_EXPORT_TOKEN"\n'


@pytest.mark.parametrize(
    ("prod_env", "saved"),
    [
        ("SHOP_EXPORT_TOKEN=t0k3n\n", {"url": "https://shop-box.tail0.ts.net:8110/api/export", "header": "X-Shop-Token",
                                       "token": "t0k3n"}),
        ("SHOP_EXPORT_TOKEN=generate-with-openssl-rand-hex-32\n", None),
        ("SHOP_EXPORT_TOKEN=REPLACE_ME\n", None),
        (None, None),
    ],
    ids=["deployed", "not-generated", "placeholder", "never-deployed"],
)  # fmt: skip
def test_the_warden_gets_prods_report_export_once_prod_holds_its_token(
    make_job: Any, capsys: pytest.CaptureFixture[str], prod_env: str | None, saved: dict[str, str] | None
) -> None:
    server = Server()
    if prod_env is not None:
        server.files["/srv/shop/prod/env/shop.env"] = ("deploy:deploy 600", prod_env)
    job = make_job(server)
    job.config = parse_text(FACTORY_TOML + REPORTS)

    setup.run(job)
    assert json.loads((job.home / "server.json").read_text()).get("reports") == saved
    assert ("Still to do: deploy prod, then run the set-up again" in capsys.readouterr().out) is (saved is None)


@pytest.mark.parametrize(
    ("line", "problem"),
    [
        ("port = 0", "[reports].port"),
        ('path = "api/export"', "[reports].path"),
        ('header = "X Shop Token"', "[reports].header"),
        ('token_key = "export_token"', "[reports].token_key"),
    ],
)
def test_a_report_export_factory_toml_cannot_name_is_refused(line: str, problem: str) -> None:
    key = line.split(" =")[0]
    kept = "".join(row + "\n" for row in REPORTS.splitlines() if not row.startswith(f"{key} ="))
    with pytest.raises(ConfigError) as refused:
        parse_text(FACTORY_TOML + kept + line + "\n")
    assert [p for p in refused.value.problems if p.startswith(problem)]


@pytest.mark.parametrize(
    ("folders", "refused"),
    [
        ('["job-store/*/steps", "jobs"]', False),
        ("[]", True),
        ('["*/agent"]', True),  # a folder starts with one of prod's volumes
        ('["job-store/../env"]', True),
        ('["/srv/shop/prod/env"]', True),
    ],
)
def test_records_name_folders_inside_prods_volumes(folders: str, refused: bool) -> None:
    text = f"{FACTORY_TOML}\n[records]\nfolders = {folders}\n"
    if not refused:
        assert parse_text(text).records == ("job-store/*/steps", "jobs")
        return
    with pytest.raises(ConfigError) as error:
        parse_text(text)
    assert [p for p in error.value.problems if p.startswith("[records].folders")], error.value.problems


@pytest.mark.parametrize(
    ("table", "where"),
    [
        ("[smoke]\ntimeout_seconds = 60\n", "factory.toml.smoke"),
        ('[[release]]\nkind = "chrome-web-store"\n', "factory.toml.release"),
        ('[workspaces.staging]\ndomain = "staging.acme.dev"\nport = 8101\nafter_deploy = []\n', "[workspaces].staging"),
    ],
    ids=["smoke", "release", "staging"],
)
def test_a_factory_toml_with_a_table_the_factory_does_not_read_is_refused_as_unknown(table: str, where: str) -> None:
    with pytest.raises(ConfigError) as refused:
        parse_text(FACTORY_TOML + "\n" + table)
    assert [p for p in refused.value.problems if p.startswith(f"{where}: unknown key")], refused.value.problems
