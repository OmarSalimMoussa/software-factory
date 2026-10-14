"""The warden's stop hook pauses the alerts only for a stop someone asked for, never for a crash."""

from __future__ import annotations

from pathlib import Path

import pytest

from runtime.shared.paths import Places
from runtime.system import pause_checks


@pytest.mark.parametrize(
    ("argv", "result", "paused"),
    [
        (["stop"], "success", True),
        (["stop"], "exit-code", False),
        (["stop"], "signal", False),
        (["stop"], "", False),
        (["pre"], "", True),
        (["post"], "", False),
    ],
    ids=["asked-stop", "crash", "killed", "unknown", "sleep", "wake"],
)
def test_only_a_clean_stop_or_a_sleep_pauses_the_alerts_and_a_wake_is_noted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, argv: list[str], result: str, paused: bool
) -> None:
    calls: list[str] = []
    places = Places.under(tmp_path)
    places.state.mkdir(parents=True)
    monkeypatch.setattr(pause_checks.healthchecks, "pause", lambda state, settings: calls.append("pause") or [])
    monkeypatch.setattr(pause_checks.settings, "load", lambda defaults, overrides: None)
    assert pause_checks.main(argv, result or None, places) == 0
    assert calls == (["pause"] if paused else [])
    assert places.woke.exists() is (argv == ["post"])
