from __future__ import annotations

import json
import logging
import urllib.request
from pathlib import Path

import pytest
from warden_fakes import defaults_text

from runtime.dashboard import server
from runtime.shared import error_reports, settings
from runtime.shared.paths import Places
from runtime.warden import public, wardend

DSN = "http://abc123@127.0.0.1:8200/7"


def _reporter(tmp_path: Path, send: error_reports.Send) -> error_reports.Reporter:
    places = Places.under(tmp_path)
    error_reports.dsn_path(places).parent.mkdir(parents=True)
    error_reports.dsn_path(places).write_text(DSN + "\n")
    places.install.mkdir()
    (places.install / "COMMIT").write_text("0123456789abcdef0123456789abcdef01234567\n")
    reporter = error_reports.install(places, timeout=3, send=send)
    assert reporter is not None
    logging.getLogger().removeHandler(reporter)  # each test logs to it alone
    return reporter


def _fail() -> None:
    try:
        int("x")
    except ValueError as exc:
        raise KeyError("lookup") from exc


def test_an_error_goes_out_as_one_envelope_in_sentrys_wire_format(tmp_path: Path) -> None:
    sent: list[tuple[urllib.request.Request, float]] = []
    reporter = _reporter(tmp_path, lambda request, timeout: sent.append((request, timeout)))
    log = logging.getLogger("factory.warden.test")
    log.addHandler(reporter)
    try:
        _fail()
    except KeyError:
        log.exception("a pass failed (%s)", "KeyError")
    finally:
        log.removeHandler(reporter)

    [(request, timeout)] = sent
    assert (request.full_url, request.get_method(), timeout) == ("http://127.0.0.1:8200/api/7/envelope/", "POST", 3)
    assert request.get_header("Content-type") == "application/x-sentry-envelope"
    assert request.get_header("X-sentry-auth") == (
        "Sentry sentry_version=7, sentry_client=software-factory/1, sentry_key=abc123"
    )
    assert isinstance(request.data, bytes)
    header, item, payload, end = request.data.split(b"\n")
    event = json.loads(payload)
    assert json.loads(header)["event_id"] == event["event_id"] and len(event["event_id"]) == 32
    assert json.loads(item) == {"type": "event", "length": len(payload), "content_type": "application/json"}
    assert end == b""
    assert {k: event[k] for k in ("level", "logger", "logentry", "release", "environment")} == {
        "level": "error",
        "logger": "factory.warden.test",
        "logentry": {"formatted": "a pass failed (KeyError)"},
        "release": "0123456789abcdef0123456789abcdef01234567",
        "environment": "factory",
    }
    cause, raised = event["exception"]["values"]  # oldest first: the cause, then what was raised from it
    assert (cause["type"], raised["type"], raised["value"]) == ("ValueError", "KeyError", "'lookup'")
    assert raised["stacktrace"]["frames"][-1]["function"] == "_fail"


def test_a_failed_send_never_raises_and_says_so_once_per_streak(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def refused(request: urllib.request.Request, timeout: float) -> None:
        raise ConnectionRefusedError(111, "Connection refused")

    reporter = _reporter(tmp_path, refused)
    log = logging.getLogger("factory.dashboard.test")
    log.addHandler(reporter)
    try:
        for n in range(3):
            log.error("the page broke %d", n)
    finally:
        log.removeHandler(reporter)

    lines = [line for line in capsys.readouterr().err.splitlines() if line.startswith("error-reports:")]
    assert lines == [
        "error-reports: an error could not be sent to GlitchTip (ConnectionRefusedError); later ones stay quiet "
        "until a send works"
    ]


def test_without_the_dsn_the_service_refuses_to_start_naming_the_file(tmp_path: Path) -> None:
    with pytest.raises(error_reports.DsnError) as refused:
        error_reports.install(Places.under(tmp_path), timeout=3)

    assert str(refused.value) == (
        f"{tmp_path}/state/secrets/factory-dsn is missing: the address of the factory's own GlitchTip, which the "
        "install writes. Install the factory again."
    )


@pytest.mark.parametrize("service", ["warden", "dashboard"])
def test_without_the_dsn_the_warden_and_the_dashboard_do_not_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, service: str
) -> None:
    places = Places.under(tmp_path)
    places.install.mkdir(parents=True)
    places.defaults.write_text(defaults_text())
    places.state.mkdir(parents=True)
    public.write_settings(places, settings.load(places.defaults, places.overrides))
    monkeypatch.setattr(Places, "system", staticmethod(lambda: places))
    main = wardend.main if service == "warden" else server.main

    with caplog.at_level(logging.CRITICAL):
        assert main() == 2
    assert f"{service}: it does not start without the factory's own GlitchTip" in caplog.text


@pytest.mark.parametrize(
    ("dsn", "url"),
    [
        ("http://k@127.0.0.1:8200/7", "http://127.0.0.1:8200/api/7/envelope/"),
        ("https://k@errors.example.com/sentry/12/", "https://errors.example.com/sentry/api/12/envelope/"),
        ("http://127.0.0.1:8200/7", None),  # no public key
        ("http://k@127.0.0.1:8200/", None),  # no project
        ("ftp://k@host/7", None),
    ],
)
def test_a_dsn_gives_the_envelope_address_or_fails_loudly(dsn: str, url: str | None) -> None:
    if url is None:
        with pytest.raises(error_reports.DsnError):
            error_reports.parse_dsn(dsn)
    else:
        assert error_reports.parse_dsn(dsn).envelope_url == url
