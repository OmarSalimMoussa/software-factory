from __future__ import annotations

import importlib.util
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 9, 14, 2, 11, tzinfo=UTC)
LIMITS = {
    "five_hour": {"used_percentage": 12, "resets_at": 1791558000},
    "seven_day": {"used_percentage": 20.5, "resets_at": 1791907200},
}


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "plugin" / "hooks" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


statusline = _load("statusline")


def _run(status: dict[str, object], out: Path) -> str:
    stdout = io.StringIO()
    statusline.run(io.StringIO(json.dumps(status)), stdout, out, NOW)
    return stdout.getvalue()


def test_writes_the_windows_with_its_own_time(tmp_path: Path) -> None:
    out = tmp_path / "cache" / "plan.json"
    # Claude Code's input carries times of its own; the reading is aged only by the script's clock.
    printed = _run({"model": {"display_name": "Opus"}, "rate_limits": LIMITS, "cost": {"total_duration_ms": 5}}, out)

    assert json.loads(out.read_text()) == {**LIMITS, "written_at": "2026-10-09T14:02:11Z"}
    assert printed.strip() == "Opus · 5h 12% · 7d 20.5%"
    assert [p.name for p in out.parent.iterdir()] == ["plan.json"]


@pytest.mark.parametrize(
    "status",
    [
        {"model": {"display_name": "Opus"}},
        {"rate_limits": None},
        {"rate_limits": {"five_hour": {"used_percentage": "12"}, "seven_day": {}}},
    ],
)
def test_input_without_readable_limits_leaves_the_last_reading(tmp_path: Path, status: dict[str, object]) -> None:
    out = tmp_path / "plan.json"
    out.write_text('{"written_at": "earlier"}')

    printed = _run(status, out)

    assert out.read_text() == '{"written_at": "earlier"}'
    assert "plan limits not read yet" in printed


def test_a_failed_write_keeps_the_old_reading_whole(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "plan.json"
    out.write_text('{"written_at": "earlier"}')

    def broken_dump(*_: object, **__: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(statusline.json, "dump", broken_dump)
    with pytest.raises(OSError):
        _run({"rate_limits": LIMITS}, out)

    assert out.read_text() == '{"written_at": "earlier"}'
    assert [p.name for p in tmp_path.iterdir()] == ["plan.json"]


def test_a_failure_prints_a_fallback_and_never_fails_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))

    assert statusline.main(["--out", str(tmp_path / "plan.json")]) == 0
    assert capsys.readouterr().out.strip() == "plan limits unknown (JSONDecodeError)"
