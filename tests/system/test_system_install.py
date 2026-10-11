from __future__ import annotations

from pathlib import Path

import pytest
from system_fakes import dry_job, settings

from runtime.shared.models import BENCH_PENDING, DEPRIORITIZED, RETIRED, REVIVING, WAITING, WORKING, Session
from runtime.shared.protocol import Refused
from runtime.shared.settings import SettingsError
from runtime.system import install, steps_machine
from runtime.system.host import DryHost
from runtime.warden import registry


def test_the_dry_run_lists_every_step_in_order_and_changes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    job = dry_job(tmp_path)

    install.run(job)

    printed = [line for line in capsys.readouterr().out.splitlines() if line[:3].strip().rstrip(".").isdigit()]
    assert printed == [f"{n:>2}. {title}" for n, (title, _) in enumerate(install.STEPS, 1)]
    assert any(action.startswith("write: /etc/systemd/system/software-factory.slice") for action in job.host.actions)
    # The owner's own git hooks read the settings published under it; no factory user may list or enter it.
    assert f"make: {tmp_path / 'root' / 'state'}/ (710, software-warden:owner)" in job.host.actions
    assert not (tmp_path / "root").exists()


def test_the_shared_services_are_placed_on_rootless_docker_and_keyed_before_the_factory_starts(
    tmp_path: Path,
) -> None:
    job = dry_job(tmp_path)

    install.run(job)

    titles = [title for title, _ in install.STEPS]
    order = ["Set up the stack user's rootless Docker", "Place the shared services", "Install the units",
             "Start the shared services", "Keep the shared services' keys", "Start the factory"]  # fmt: skip
    assert [titles.index(title) for title in order] == sorted(titles.index(title) for title in order)
    root = tmp_path / "root"
    for action in (
        f"write: {root}/services/services.env (640, root:software-stack)",
        "run: systemctl reload-or-restart software-services.service",
        f"write: {root}/state/secrets/laptop-glitchtip.json (600, software-warden:software-warden)",
        f"write: {root}/state/secrets/laptop-langfuse.json (600, software-warden:software-warden)",
        f"write: {root}/state/secrets/factory-dsn (640, software-warden:software-dashboard)",
    ):
        assert isinstance(job.host, DryHost) and action in job.host.actions


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

    steps_machine.secrets(job)

    assert [a.removeprefix("ask the owner: ") for a in host.actions if a.startswith("ask the owner: ")] == asked
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
