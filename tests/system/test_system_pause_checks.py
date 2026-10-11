"""The warden's stop hook pauses the alerts only for a stop someone asked for, never for a crash."""

from __future__ import annotations

import pytest

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
def test_only_a_clean_stop_or_a_sleep_pauses_the_alerts(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], result: str, paused: bool
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(pause_checks.healthchecks, "pause", lambda state, settings: calls.append("pause") or [])
    monkeypatch.setattr(pause_checks.settings, "load", lambda defaults, overrides: None)
    environ = {"SERVICE_RESULT": result} if result else {}
    assert pause_checks.main(argv, environ) == 0
    assert calls == (["pause"] if paused else [])
