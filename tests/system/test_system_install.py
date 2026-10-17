from __future__ import annotations

import functools
import json
import re
import subprocess
from pathlib import Path

import pytest
from system_fakes import FACTORY_ORIGIN, PUBLIC_HOST, dry_job, settings

from runtime.shared import network, upgrades
from runtime.shared.models import BENCH_PENDING, DEPRIORITIZED, RETIRED, REVIVING, WAITING, WORKING, Session
from runtime.shared.protocol import Refused
from runtime.shared.settings import SettingsError
from runtime.system import factory_repo, install, layout, root_commits, uninstall
from runtime.system.host import CommandError, DryHost
from runtime.system.install_steps import steps_machine, steps_prepare, steps_secrets, steps_wire
from runtime.system.job import REPO_ROOT, Install
from runtime.system.journal import Journal
from runtime.system.real_host import RealHost
from runtime.warden.delivery import pins
from runtime.warden.sessions import registry, session_settings
from standard.server import edge as server_edge


def _answered(job: Install) -> None:
    """Every secret and key in place already, so a run asks the owner nothing."""
    assert isinstance(job.host, DryHost)
    folder = job.places.state / "secrets"
    job.host.paths |= {job.places.totp_secret, folder / "deploy.key", folder / "factory-read.key.pub",
                       folder / "github.token", folder / "claude.token"}  # fmt: skip
    job.host.files[job.owner.home / layout.OWNER_SERVER_KEYS] = "{}"


def test_the_phases_hold_every_step_once_and_in_order() -> None:
    steps = [step for phase in install.PHASES for _, step in phase.steps]

    assert steps == [step for _, step in install.STEPS] and len(set(steps)) == len(steps)
    assert all(phase.steps for phase in install.PHASES)
    assert len({phase.title for phase in install.PHASES}) == len(install.PHASES)


@pytest.mark.parametrize("verbose", [False, True], ids=["plain", "verbose"])
def test_the_terminal_shows_only_the_phases_unless_verbose_and_a_dry_run_changes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], verbose: bool
) -> None:
    job = dry_job(tmp_path)
    _answered(job)
    assert isinstance(job.host, DryHost)
    job.host.journal = Journal(verbose=verbose)

    install.run(job)

    out = capsys.readouterr().out.splitlines()
    phases = [f"  \u2713 {phase.title}" for phase in install.PHASES]
    if verbose:
        assert [line for line in out if line in phases] == phases
        assert any("would run: systemctl" in line for line in out)
    else:
        assert out == phases
    assert any(action.startswith("write: /etc/systemd/system/software-factory.slice") for action in job.host.actions)
    # The owner's own git hooks read the settings published under it; no factory user may list or enter it.
    assert f"make: {tmp_path / 'root' / 'state'}/ (710, software-warden:owner)" in job.host.actions
    assert not (tmp_path / "root").exists()


def test_a_failure_shows_one_plain_sentence_and_the_log_keeps_the_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(ctx: Install) -> None:
        ctx.host.run(["false", "--secret-flag"])

    phases = (install.Phase("Checking this computer", (("Checking the tools", lambda ctx: None),)),
              install.Phase("Preparing this computer", (("Installing the system packages", broken),)))  # fmt: skip
    monkeypatch.setattr(install, "PHASES", phases)
    job = dry_job(tmp_path)
    log = tmp_path / "install.log"
    job.host = RealHost(Journal(log))

    problem = install.perform(job, keep=True)

    out = capsys.readouterr().out
    assert problem == ("The install could not finish installing the system packages: a command it ran failed. Run it "
                       "again; if it stops here again, the log says why.")  # fmt: skip
    assert out.splitlines() == ["  \u2713 Checking this computer", "  \u2717 Preparing this computer", "", problem]
    kept = log.read_text(encoding="utf-8")
    assert "$ false --secret-flag" in kept and "exit 1 after" in kept and "--secret-flag" not in out


def _session(state: str) -> Session:
    return Session(
        id="s1",
        name="impl-cart",
        role="implementer",
        family="impl",
        model="m",
        effort="high",
        task="t-1",
        parent="",
        worktree="/w",
        state=state,
        started="2026-10-09T00:00:00Z",
        started_by="o",
    )


@pytest.mark.parametrize("state", [WORKING, WAITING, BENCH_PENDING, REVIVING])
def test_a_reinstall_is_refused_while_a_session_is_live(tmp_path: Path, state: str) -> None:
    job = dry_job(tmp_path)
    registry.write(job.places, {"s1": _session(state)})

    with pytest.raises(Refused, match="impl-cart"):
        install.run(job)


@pytest.mark.parametrize("state", [DEPRIORITIZED, RETIRED])
def test_deprioritized_and_retired_sessions_let_a_reinstall_through(tmp_path: Path, state: str) -> None:
    job = dry_job(tmp_path)
    registry.write(job.places, {"s1": _session(state)})

    install.run(job)


def test_the_factory_is_never_installed_onto_its_own_repo(tmp_path: Path) -> None:
    job = dry_job(tmp_path)
    job.host.answers[f"git -C {job.product} remote get-url origin"] = (0, "https://github.com/acme/software-factory")

    with pytest.raises(Refused, match="never works on itself"):
        install.run(job)


def test_root_never_opens_a_repository_the_owner_owns(tmp_path: Path) -> None:
    """Git refuses root a repository another user owns: each clone of one comes from a bundle the owner writes."""
    job = dry_job(tmp_path)

    install.run(job)

    assert isinstance(job.host, DryHost) and job.factory is not None
    theirs = (f" {job.product}", f" {job.factory}")
    reads = [action.split(" | ")[0] for action in job.host.actions if action.startswith("run: git")]
    reads = [read for read in reads if any(f"{path} " in f"{read} " for path in theirs)]
    assert sum("bundle create" in read for read in reads) == 1  # the product's; the factory is read as files only
    assert all(read.endswith(f"(as {job.owner.name})") for read in reads)


@pytest.mark.parametrize("found", ["", "directory", "symbolic link"], ids=["missing", "a-folder", "a-link"])
def test_the_install_gives_the_warden_its_queue_in_the_agents_checkout_and_never_follows_a_link(
    tmp_path: Path, found: str
) -> None:
    job = dry_job(tmp_path)
    assert isinstance(job.host, DryHost)
    queue = Path(settings().get_str("repo.path")) / "queue"
    job.host.answers[f"stat -c %F {queue}"] = (0, found) if found else (1, "")

    if found == "symbolic link":
        with pytest.raises(Refused, match="not a folder"):
            install.run(job)
        assert not any(action.startswith(f"make: {queue}/") for action in job.host.actions)
    else:
        install.run(job)
        assert f"make: {queue}/ (755, software-warden:software-warden)" in job.host.actions


@pytest.mark.parametrize(
    "cache", ["/var/cache/uv", "../software-stack/.cache", ".", ".claude/cache", ".config/git", "bin"]
)
def test_a_writable_cache_must_be_a_folder_of_the_agents_home_no_check_could_escape_through(cache: str) -> None:
    with pytest.raises(SettingsError, match="checks.writable_caches"):
        settings(**{"checks.writable_caches": [".npm", cache]})


@pytest.mark.parametrize(
    ("low", "high"),
    [
        ("vitals.memory_red_gib", "vitals.memory_amber_gib"),
        ("vitals.battery_red", "vitals.battery_amber"),
        ("cost.plan_amber_percent", "cost.plan_red_percent"),
    ],
)
def test_a_lower_limit_must_stay_below_the_one_above_it(low: str, high: str) -> None:
    section, key = high.split(".")
    with pytest.raises(SettingsError, match=f"{low} must be below {high}"):
        settings(**{low: settings().tree()[section][key]})


@pytest.mark.parametrize(
    "changes",
    [{"budget.slice_memory_high": "10G"}, {"budget.test_memory_ceiling_mib": 8192}, {"budget.test_reserve_mib": 6144}],
)
def test_a_budget_with_no_room_for_one_check_running_tests_is_refused(changes: dict[str, object]) -> None:
    with pytest.raises(SettingsError, match="no check could run the tests"):
        settings(**changes)


WARDEN_OWNED = "(600, software-warden:software-warden)"


@pytest.mark.parametrize(
    ("dry", "present", "asked", "written"),
    [
        # A first install asks for the token and a name; the dry run shows it with its placeholder answers.
        (True, {}, ["The token `claude setup-token` printed, for the agents' sessions:",
                    "A name for this Claude account (Enter for main):"],
         ["claude-accounts/<answer>.token", "claude.token"]),
        (False, {"claude.token": "sk-ant-oat01-kept-from-before-a1b2\n"}, [], []),
    ],
)  # fmt: skip
def test_the_secrets_step_keeps_the_agents_claude_accounts(
    tmp_path: Path, dry: bool, present: dict[str, str], asked: list[str], written: list[str]
) -> None:
    job = dry_job(tmp_path)
    job.settings = settings()
    host, folder = job.host, job.places.state / "secrets"
    assert isinstance(host, DryHost)
    host.dry = dry
    host.paths |= {
        job.places.totp_secret,
        folder / "deploy.key",
        folder / "factory-read.key.pub",
        folder / "github.token",
        upgrades.token_path(job.places),
    }
    host.files.update({folder / name: text for name, text in present.items()})

    steps_secrets.secrets(job)

    # Without a signed-in GitHub CLI, a real install also asks the owner to sign in; that is not this test's question.
    questions = [a.removeprefix("ask the owner: ") for a in host.actions if a.startswith("ask the owner: ")]
    assert [q for q in questions if q != "Sign in to GitHub"] == asked
    assert [a for a in host.actions if a.startswith("write: ")] == [
        f"write: {folder}/{w} {WARDEN_OWNED}" for w in written
    ]


@pytest.mark.parametrize(
    ("kept", "github", "refused"),
    [(True, 200, False), (False, 200, False), (False, 404, True)],
    ids=["kept-from-before", "asked-and-checked", "one-github-refuses"],
)
def test_the_install_asks_once_for_a_factory_token_that_github_lets_read_the_repo_and_root_alone_keeps_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kept: bool, github: int, refused: bool
) -> None:
    job = dry_job(tmp_path)
    job.settings = settings(**{"factory.github": "acme/software-factory"})
    host, folder, mine = job.host, job.places.state / "secrets", upgrades.token_path(job.places)
    assert isinstance(host, DryHost)
    host.dry = False  # decides like a real install; a dry host still changes nothing
    host.paths |= {job.places.totp_secret, folder / "deploy.key", folder / "factory-read.key.pub",
                   folder / "github.token", folder / "claude.token"}  # fmt: skip
    if kept:
        host.paths.add(mine)
    asked: list[tuple[str, str | None]] = []

    def get(url: str, headers: dict[str, str], timeout: float) -> network.Answer:
        asked.append((url, headers.get("Authorization")))
        return network.Answer(github, {}, b"{}")

    monkeypatch.setattr(factory_repo, "http_get", get)
    if refused:
        with pytest.raises(Refused, match="That token cannot read the factory's repo"):
            steps_secrets.secrets(job)
    else:
        steps_secrets.secrets(job)

    questions = [a for a in host.actions if a.startswith("ask the owner: A read-only GitHub token for the factory")]
    wrote = [a for a in host.actions if a.startswith(f"write: {mine} (600, root:root)")]
    checked = [] if kept else [("https://api.github.com/repos/acme/software-factory", "Bearer <answer>")]
    assert (len(questions), asked, len(wrote)) == (0 if kept else 1, checked, 0 if kept or refused else 1)
    assert not [a for a in host.actions if a.startswith(f"write: {folder}/factory.token")]  # never the warden's


@pytest.mark.parametrize(
    ("first", "reached", "keep", "rolled"),
    [
        (True, install.FIRST_CHANGE, False, True),
        (False, install.FIRST_CHANGE + 5, False, False),  # a reinstall: removing it would delete real work
        (True, install.FIRST_CHANGE - 1, False, False),  # nothing was changed yet
        (True, install.FIRST_CHANGE + 5, True, False),  # the owner keeps it to look at
    ],
    ids=["first-install", "reinstall", "before-any-change", "kept"],
)
def test_only_a_failed_first_install_is_taken_back(
    tmp_path: Path, first: bool, reached: int, keep: bool, rolled: bool
) -> None:
    job = dry_job(tmp_path)
    job.host.dry = False  # the decision for a real run; nothing here runs a step
    job.first, job.reached = first, reached

    assert install.rolls_back(job, keep=keep) is rolled


def test_a_saved_value_for_a_setting_the_factory_no_longer_has_is_dropped_and_the_rest_kept(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    job = dry_job(tmp_path)
    assert isinstance(job.host, DryHost)
    job.host.files[job.places.overrides] = '[factory]\nclone = "/var/lib/old"\n\n[caps]\nimplementers = 2\n'
    steps_prepare.check_machine(job)

    steps_prepare.settle_settings(job)

    assert job.settings is not None and job.settings.get_int("caps.implementers") == 2
    assert "factory.clone" not in job.settings.overrides and job.settings.get_str("factory.github")
    assert "Dropped your saved factory.clone: this factory has no such setting." in capsys.readouterr().out


def test_the_dashboard_goes_on_the_edge_by_its_public_names_and_the_uninstall_takes_it_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = dry_job(tmp_path)
    assert isinstance(job.host, DryHost)
    steps_prepare.check_machine(job)
    steps_prepare.settle_settings(job)
    written: dict[Path, str] = {}
    monkeypatch.setattr(job.host, "write", lambda path, text, **kw: written.__setitem__(path, text))
    reload = f"run: {' '.join(server_edge.RELOAD)}"

    steps_wire.edge(job)

    port = settings().get_int("dashboard.port")
    lines = written[layout.EDGE_SITE].splitlines()
    assert f"{PUBLIC_HOST} {{" in lines and f"\treverse_proxy 127.0.0.1:{port} {{" in lines
    assert job.host.actions == [reload]
    assert steps_wire.dashboard_address(job.need_settings().get_list("dashboard.hosts")) == f"https://{PUBLIC_HOST}/"
    job.host.paths.add(layout.EDGE_SITE)
    job.host.dry = False  # decides like a real uninstall; a dry host still changes nothing
    asked: list[str] = []
    answer = job.host.query
    monkeypatch.setattr(job.host, "query", lambda argv, **kw: asked.append(" ".join(argv)) or answer(argv, **kw))

    uninstall.programs(job.host)

    assert [a for a in job.host.actions if str(server_edge.SITES) in a] == [f"remove: {layout.EDGE_SITE}"]
    assert asked == [" ".join(server_edge.RELOAD)]


def test_a_dashboard_site_the_edge_refuses_comes_off_the_edge_before_the_install_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = dry_job(tmp_path)
    steps_prepare.check_machine(job)
    steps_prepare.settle_settings(job)
    done: list[str] = []

    def run(argv: list[str], **kw: object) -> str:
        done.append("reload")
        if done == ["reload"]:
            raise CommandError(argv, 1, "Error: adapting config using caddyfile: ambiguous site definition")
        return ""

    monkeypatch.setattr(job.host, "run", run)
    monkeypatch.setattr(job.host, "remove", lambda path: done.append(f"remove {path}"))

    with pytest.raises(Refused, match="The edge refused the dashboard's site, which is off the edge again"):
        steps_wire.edge(job)

    assert done == ["reload", f"remove {layout.EDGE_SITE}", "reload"]


@pytest.mark.parametrize(
    ("edge", "hosts", "said"),
    [
        (False, None, f"it has no edge to publish the dashboard on: {server_edge.SITES}, which the server set-up"),
        (True, ["127.0.0.1", "localhost"], "dashboard.hosts names no public address for the dashboard"),
    ],
    ids=["no-edge", "no-public-name"],
)
def test_an_install_refuses_a_box_without_an_edge_or_a_public_name_before_it_changes_anything(
    tmp_path: Path, edge: bool, hosts: list[str] | None, said: str
) -> None:
    job = dry_job(tmp_path)
    assert isinstance(job.host, DryHost)
    if not edge:
        job.host.paths.discard(server_edge.SITES)
    if hosts is not None:
        job.changes["dashboard.hosts"] = hosts

    with pytest.raises(Refused, match=re.escape(said)):
        install.run(job)

    assert job.reached < install.FIRST_CHANGE and job.host.actions == []


@pytest.mark.parametrize(
    ("meta", "refused"),
    [({"ssh_keys": ["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"]}, False),
     ({"ssh_keys": []}, True), (OSError("no network"), True)],
    ids=["published", "none-named", "unreachable"],
)  # fmt: skip
def test_the_warden_trusts_github_only_by_the_host_keys_github_publishes_over_https(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, meta: object, refused: bool
) -> None:
    job = dry_job(tmp_path)
    assert isinstance(job.host, DryHost)
    job.settings = settings()
    job.host.dry = False  # decides like a real install; a dry host still changes nothing

    def get(url: str, timeout: float) -> bytes:
        assert url == "https://api.github.com/meta"
        if isinstance(meta, Exception):
            raise meta
        return json.dumps(meta).encode()

    monkeypatch.setattr(steps_machine, "_get", get)
    written: dict[Path, str] = {}
    monkeypatch.setattr(job.host, "write", lambda path, text, **kw: written.__setitem__(path, text))
    if refused:
        with pytest.raises(Refused, match="GitHub"):
            steps_machine.github_host_keys(job)
        assert written == {}
    else:
        steps_machine.github_host_keys(job)
        assert written[job.places.github_known_hosts].startswith("github.com ssh-ed25519 AAAA")


def _product(tmp_path: Path) -> Path:
    """The agents' checkout of a product on an older factory: its pin, an old workflow and the retired heartbeat."""
    repo = tmp_path / "agents-shop"
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "factory-ref").write_text(f"acme/software-factory@{'a' * 40}\n")
    (repo / ".github" / "workflows" / "ci.yml").write_text("old ci\n")
    (repo / ".github" / "workflows" / "heartbeat.yml").write_text("old heartbeat\n")
    (repo / "app.py").write_text("print('shop')\n")
    for argv in (["init", "-q", "-b", "master"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t@invalid",
                                                                  "commit", "-q", "-m", "The shop"]):  # fmt: skip
        subprocess.run(["git", "-C", str(repo), *argv], check=True)
    return repo


@pytest.mark.parametrize(
    ("first", "dirty", "moves"),
    [(False, False, True), (True, False, False), (False, True, False)],
    ids=["an-upgrade-moves-it", "a-first-install-leaves-it", "uncommitted-work-leaves-it"],
)
def test_an_upgrade_moves_the_agents_product_to_its_factory_in_one_commit_root_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first: bool, dirty: bool, moves: bool
) -> None:
    repo = _product(tmp_path)
    before = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
    if dirty:
        (repo / "app.py").write_text("print('half done')\n")
    job = dry_job(tmp_path)
    job.host = RealHost(Journal(terminal=False))
    job.settings = settings(**{"repo.path": str(repo)})
    job.first, job.sha, job.factory_origin, job.stage = first, "e" * 40, FACTORY_ORIGIN, REPO_ROOT
    monkeypatch.setattr(root_commits, "AGENT_USER", "")  # this test's user stands in for the agent's
    monkeypatch.setattr(job.host, "write", lambda path, text, **how: (path.parent.mkdir(parents=True, exist_ok=True),
                                                                      path.write_text(text)))  # fmt: skip

    steps_wire.product_pin(job)

    git = functools.partial(lambda *argv: subprocess.run(["git", "-C", str(repo), *argv], capture_output=True,
                                                         text=True, check=True).stdout)  # fmt: skip
    head = git("rev-parse", "HEAD")
    if not moves:
        assert head == before and not upgrades.pins_path(job.places).exists()
        return
    assert (repo / ".github" / "factory-ref").read_text() == f"acme/software-factory@{'e' * 40}\n"
    assert (repo / ".github" / "workflows" / "ci.yml").read_text() == (REPO_ROOT / "templates/ci/ci.yml").read_text()
    assert not (repo / ".github" / "workflows" / "heartbeat.yml").exists() and git("status", "--porcelain") == ""
    [pin] = upgrades.read_pins(job.places)
    assert (pin["commit"], pin["parent"]) == (head.strip(), before.strip())
    assert pins._pin(git, job.places, head.strip()) == set(pin["blobs"])  # the warden lets exactly this through


def test_the_session_settings_folder_is_the_wardens(tmp_path: Path) -> None:
    job = dry_job(tmp_path)

    steps_machine.state_tree(job)

    assert f"make: {session_settings.folder(job.places)}/ (755, software-warden:software-warden)" in job.host.actions
