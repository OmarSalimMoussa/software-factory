"""Who may use the dashboard: its own addresses only, an admitted device only, and for a change a same-origin JSON
request carrying the page's own header and an idempotency key."""

from __future__ import annotations

import json
import urllib.request

import pytest
from dashboard_rig import HOST, OWNER, Rig
from dashboard_seed import T0

from runtime.dashboard import auth, brief_frame, pictures
from runtime.dashboard.server import serve
from runtime.shared import store, totp

WRITE = {**OWNER, "origin": "http://127.0.0.1:8300", "content-type": "application/json", "x-software": "1"}


def _code(rig: Rig, step: int = 0) -> str:
    return totp.code_at(rig.places.totp_secret.read_text().strip(), int(T0 // totp.STEP_S) + step)


@pytest.mark.parametrize(
    ("method", "headers", "status"),
    [
        ("GET", {**OWNER, "host": "attacker.example"}, 403),
        ("POST", {**WRITE, "origin": "http://attacker.example"}, 403),
        ("POST", {key: v for key, v in WRITE.items() if key != "origin"}, 403),
        ("POST", {key: v for key, v in WRITE.items() if key != "x-software"}, 403),
        ("POST", {**WRITE, "content-type": "text/plain"}, 415),
        ("POST", {key: v for key, v in WRITE.items() if key != "cookie"}, 401),
        ("GET", HOST, 401),
    ],
)
def test_a_request_from_elsewhere_or_without_the_owner_is_refused(
    rig: Rig, method: str, headers: dict[str, str], status: int
) -> None:
    before = rig.places.proposals.read_text()
    if method == "GET":
        reply = rig.get("/api/home", headers)
    else:
        reply = rig.post("/api/proposals/t-0027/approve", {"idempotency_key": "k-00000001"}, headers)
    assert reply.status == status
    assert rig.places.proposals.read_text() == before


def test_a_right_code_admits_the_device_with_a_strict_owner_cookie(rig: Rig) -> None:
    sent = {key: v for key, v in WRITE.items() if key != "cookie"}
    reply = rig.post("/api/device", {"code": _code(rig), "idempotency_key": "device-0001"}, sent)
    assert reply.status == 200
    cookie = reply.headers["Set-Cookie"]
    token = cookie.split(";")[0].removeprefix("owner=")
    assert token not in reply.body.decode()
    assert {"HttpOnly", "SameSite=Strict", "Path=/", "Max-Age=2592000"} <= {p.strip() for p in cookie.split(";")}
    assert "Secure" not in cookie  # served on 127.0.0.1, plain HTTP
    assert rig.get("/api/shell", {**HOST, "cookie": f"owner={token}"}).status == 200
    public = "factory.example.com"
    tree = store.read_json(rig.places.state / "public" / "settings.json")
    tree["dashboard"]["hosts"].append(public)
    store.write_json(rig.places.state / "public" / "settings.json", tree)
    again = rig.post(
        "/api/device",
        {"code": _code(rig, 1), "idempotency_key": "device-0002"},
        {**sent, "host": public, "origin": f"https://{public}"},
    )
    assert again.headers["Set-Cookie"].endswith("; Secure")


def test_a_wrong_code_says_how_many_tries_are_left(rig: Rig) -> None:
    reply = rig.post("/api/device", {"code": "000000", "idempotency_key": "device-0001"})
    assert reply.status == 403
    assert json.loads(reply.body) == {
        "ok": False,
        "refused": "code",
        "reason": "Wrong code, 4 tries left.",
        "code": {"tries_left": 4},
    }


def test_pages_and_pictures_carry_their_policies(rig: Rig) -> None:
    server = serve(rig.app, 0, 2, 5)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    head = rig.parts("/api/task/t-0017")["task"]["data"]["testers"]["rows"]
    evidence = next(shot for row in head for shot in row["shots"])
    try:
        for path, policy, media in (
            ("/", auth.PAGE_POLICY, "text/html; charset=utf-8"),
            ("/api/picture/task/t-0027", pictures.POLICY, "image/svg+xml"),
            (evidence, pictures.POLICY, "image/svg+xml"),
            ("/api/brief-page/t-0023/1", brief_frame.POLICY, "text/html; charset=utf-8"),
        ):
            with urllib.request.urlopen(urllib.request.Request(base + path, headers={"Cookie": "owner=1"})) as answer:
                assert answer.headers["Content-Security-Policy"] == policy
                assert answer.headers["X-Content-Type-Options"] == "nosniff"
                assert answer.headers["Content-Type"] == media
        assert rig.get("/api/evidence/t-0017/../../totp.key").status == 404
        brief = rig.get("/api/brief-page/t-0023/1").body.decode()
        assert (
            brief.index(brief_frame.SCRIPT) < brief.rindex("</body>") and "sandbox allow-scripts" in brief_frame.POLICY
        )
    finally:
        server.shutdown()
        server.server_close()


def test_while_the_warden_is_away_the_last_parts_are_served_marked_old(rig: Rig) -> None:
    assert rig.get("/api/home").status == 200
    rig.warden.stop()
    reply = json.loads(rig.get("/api/home").body)
    assert reply["warden"]["ok"] is False and reply["warden"]["since"] == "11:40"
    assert reply["parts"] and all(part["ok"] and part["old"] for part in reply["parts"].values())
    stranger = rig.get("/api/home", {**HOST, "cookie": "owner=never-confirmed"})
    assert stranger.status == 503 and "parts" not in json.loads(stranger.body)
    never = rig.get("/api/guard")
    assert never.status == 503 and json.loads(never.body)["warden"]["ok"] is False
    assert rig.post("/api/factory/resume", {"idempotency_key": "resume-0001"}).status == 503
