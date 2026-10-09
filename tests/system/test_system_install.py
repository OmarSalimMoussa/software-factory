from __future__ import annotations

from pathlib import Path

import pytest
from system_fakes import dry_job

from runtime.shared.models import BENCH_PENDING, DEPRIORITIZED, RETIRED, REVIVING, WAITING, WORKING, Session
from runtime.shared.protocol import Refused
from runtime.system import install
from runtime.warden import registry


def test_the_dry_run_lists_every_step_in_order_and_changes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    job = dry_job(tmp_path)

    install.run(job)

    printed = [line for line in capsys.readouterr().out.splitlines() if line[:3].strip().rstrip(".").isdigit()]
    assert printed == [f"{n:>2}. {title}" for n, (title, _) in enumerate(install.STEPS, 1)]
    assert any(action.startswith("write: /etc/systemd/system/software-factory.slice") for action in job.host.actions)
    assert not (tmp_path / "root").exists()


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
