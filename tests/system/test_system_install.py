from __future__ import annotations

from pathlib import Path

import pytest
from system_fakes import dry_job, settings

from runtime.shared.models import BENCH_PENDING, DEPRIORITIZED, RETIRED, REVIVING, WAITING, WORKING, Session
from runtime.shared.protocol import Refused
from runtime.shared.settings import SettingsError
from runtime.system import ci_check, install, layout, steps_machine, steps_secrets, steps_services, steps_wire
from runtime.system.host import DryHost, RealHost
from runtime.system.job import Install
from runtime.system.journal import Journal
from runtime.warden import registry


def _answered(job: Install) -> None:
    """Every secret and key in place already, so a run asks the owner nothing."""
    assert isinstance(job.host, DryHost)
    folder = job.places.state / "secrets"
    job.host.paths |= {job.places.totp_secret, folder / "deploy.key", folder / "factory-read.key.pub",
                       folder / "github.token", folder / "claude-accounts" / "active"}  # fmt: skip
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


def test_the_shared_services_are_downloaded_early_and_started_and_keyed_before_the_factory_starts(
    tmp_path: Path,
) -> None:
    job = dry_job(tmp_path)

    install.run(job)

    steps = [step for _, step in install.STEPS]
    order = [steps_machine.rootless_docker, steps_services.place, steps_services.pull, steps_secrets.secrets,
             steps_wire.units, steps_services.start, steps_services.keys, steps_wire.start]  # fmt: skip
    assert [steps.index(step) for step in order] == sorted(steps.index(step) for step in order)
    root = tmp_path / "root"
    assert isinstance(job.host, DryHost)
    pulled = next(n for n, a in enumerate(job.host.actions) if a.startswith("start in the background: docker compose"))
    started = job.host.actions.index("run: systemctl reload-or-restart software-services.service")
    assert job.host.actions[pulled].endswith("pull --quiet (as software-stack)") and pulled < started
    for action in (
        f"write: {root}/services/services.env (640, root:software-stack)",
        f"write: {root}/state/secrets/laptop-glitchtip.json (600, software-warden:software-warden)",
        f"write: {root}/state/secrets/laptop-langfuse.json (600, software-warden:software-warden)",
        f"write: {root}/state/secrets/factory-dsn (640, software-warden:software-dashboard)",
    ):
        assert action in job.host.actions


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
        slot=1,
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
    assert sum("bundle create" in read for read in reads) == 2
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


def test_the_install_makes_each_tool_cache_a_session_may_write_for_the_agent(tmp_path: Path) -> None:
    job = dry_job(tmp_path)

    install.run(job)

    assert isinstance(job.host, DryHost)
    made = [action for action in job.host.actions if action.startswith("make: /home/software-agent/.")]
    agent = "(700, software-agent:software-agent)"
    assert made == [
        f"make: /home/software-agent/{folder}/ {agent}" for folder in (".claude", ".cache", ".cache/uv", ".npm")
    ]


@pytest.mark.parametrize(
    "cache", ["/var/cache/uv", "../software-stack/.cache", ".", ".claude/cache", ".config/git", "bin"]
)
def test_a_writable_cache_must_be_a_folder_of_the_agents_home_no_session_could_escape_through(cache: str) -> None:
    with pytest.raises(SettingsError, match="sandbox.writable_caches"):
        settings(**{"sandbox.writable_caches": [".npm", cache]})


WARDEN_OWNED = "(600, software-warden:software-warden)"


@pytest.mark.parametrize(
    ("dry", "present", "asked", "written"),
    [
        # A first install asks for the token and a name; the dry run shows it with its placeholder answers.
        (True, {}, ["The token `claude setup-token` printed, for the agents' sessions:",
                    "A name for this Claude account (Enter for main):"],
         ["claude-accounts/<answer>.token", "claude.token", "claude-accounts/active"]),
        # A reinstall over an install that kept one token makes it the account main, and the agents keep using it.
        (False, {"claude.token": "sk-ant-oat01-kept-from-before-a1b2\n"}, [],
         ["claude-accounts/main.token", "claude.token", "claude-accounts/active"]),
        (False, {"claude.token": "sk-ant-oat01-kept-from-before-a1b2\n", "claude-accounts/active": "work\n"}, [], []),
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
    ("first", "reached", "keep", "rolled"),
    [
        (True, install.FIRST_CHANGE, False, True),
        (False, install.FIRST_CHANGE + 5, False, False),  # a reinstall: removing it would delete real work
        (True, install.FIRST_CHANGE - 1, False, False),  # nothing was changed yet
        (True, install.FIRST_CHANGE + 5, True, False),  # the owner keeps it to look at
        # A commit whose CI has not passed is refused before anything changes.
        (True, [step for _, step in install.STEPS].index(ci_check.passed) + 1, False, False),
    ],
    ids=["first-install", "reinstall", "before-any-change", "kept", "ci-not-passed"],
)
def test_only_a_failed_first_install_is_taken_back(
    tmp_path: Path, first: bool, reached: int, keep: bool, rolled: bool
) -> None:
    job = dry_job(tmp_path)
    job.host.dry = False  # the decision for a real run; nothing here runs a step
    job.first, job.reached = first, reached

    assert install.rolls_back(job, keep=keep) is rolled
