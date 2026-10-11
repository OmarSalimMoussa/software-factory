"""The real dashboard server on a seeded state, with a fake warden, for the UI's browser drives.

Run: python3 tests/dashboard_server/live_server.py [--port 0]. It prints "listening on <url>" once ready and answers
the drives' test routes as the UI's own fixture server does: GET /__calls, POST /__reset and POST /__switch. Each
switch makes its broken state for real where the warden can (down, scenarios, bump, saved, a failing push); a
failure of the dashboard itself (fail_path, fail_part, api, refuse) is injected around the server. The clock starts
at 14:02 UTC on the fixtures' day, an orchestrator stand-in refills free proposal places, and every coded action
takes the code 123456.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import signal
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parents[1]), str(HERE.parent), str(HERE)]

from dashboard_seed import FIXTURES, iso, seed  # noqa: E402
from fake_warden import FakeWarden, GitHub  # noqa: E402

from runtime.dashboard import server  # noqa: E402
from runtime.dashboard.client import Warden  # noqa: E402
from runtime.queue import formats, propose  # noqa: E402
from runtime.shared import models, store  # noqa: E402
from runtime.shared import settings as settings_module  # noqa: E402
from runtime.warden import crashwatch, deploys, ops, public, registry, settings_save  # noqa: E402
from runtime.warden.context import Caller, Clock  # noqa: E402

CODE = "123456"
START = 1_791_554_520.0  # 2026-10-09 14:02:00 UTC
ORCHESTRATOR = Caller(kind="agent", session="orchestrator", role="orchestrator")
WARDEN = Caller(kind="warden", role="warden")
REFILL_SECONDS = 4.0  # the stand-in thinks this long, so the page shows the place choosing first


class Running(Clock):
    """The fixtures' afternoon, moving at the speed of the wall clock."""

    def __init__(self) -> None:
        self.zero, self.skipped = time.monotonic(), 0.0

    def now(self) -> float:
        return START + time.monotonic() - self.zero + self.skipped

    def skip(self, seconds: float) -> None:
        """Let time pass at once: a wait the drives cannot sit through, such as a bench's grace."""
        self.skipped += seconds


class Deploys(GitHub):
    """GitHub whose push workflow redeploys prod: prod then answers on the new build, which the warden's tick would
    log, unless told to fail."""

    def __init__(self, live: Live) -> None:
        self.live = live

    def run(self, run_id: str) -> dict[str, Any]:
        if self.live.switches.get("op_fail"):
            return {
                "id": run_id,
                "status": "completed",
                "conclusion": "failure",
                "url": "https://github.invalid/r",
                "jobs": {"deploy": {"status": "completed", "conclusion": "failure"}},
            }
        sha = store.read_json(self.live.places.state / "push.json")["sha"]
        at = iso(self.live.clock.now() + 2)
        store.append_jsonl(self.live.places.probes, models.dump(models.Probe(at, "prod", True, sha, 80)))
        deploys.record(self.live.places, "prod", sha, at)
        return super().run(run_id)


class Live:
    def __init__(self) -> None:
        self.clock = Running()
        self.calls: list[dict[str, Any]] = []
        self.switches: dict[str, Any] = {}
        self.warden: FakeWarden | None = None
        self.roots: list[Path] = []
        self.timers: list[threading.Timer] = []
        self._launch = ops.launch
        ops.launch = self.paced  # type: ignore[assignment]  # the drives set how long each step of an operation takes
        self.app = self.fresh()

    def paced(self, ctx: Any, kind: str, steps: Any, result: dict[str, Any] | None = None) -> str:
        """An operation whose every step takes at least the drive's op_ms, so a drive can reload the page while it
        still runs, however fast or slow the machine."""
        wait = self.switches.get("op_ms", 0) / 1000

        def slow(act: Any) -> Any:
            return lambda: (time.sleep(wait), act())[1]

        return self._launch(ctx, kind, [(text, slow(act)) for text, act in steps], result)

    def remove(self, *_: Any) -> None:
        """The drive stops the server with SIGTERM: every seeded folder goes, once no stand-in still writes to it."""
        for timer in self.timers:
            timer.cancel()
            timer.join()
        self.warden = None
        for root in self.roots:
            shutil.rmtree(root, ignore_errors=True)
        raise SystemExit(0)

    def fresh(self) -> server.App:
        """The seeded afternoon again."""
        if self.warden is not None:
            self.warden.stop()
        self.switches = {"fail_part": [], "refuse": {}}
        self.reserve = [("t-0018", "A quick win for G3, asked for twice.")]
        root = Path(tempfile.mkdtemp(prefix="dashboard-live-"))
        self.roots.append(root)
        places = seed(root, self.clock.now())
        self.places, self.repo = places, root / "repo"
        for folder in ("plugin", "procedures", "rules"):
            shutil.copytree(HERE.parents[1] / folder, places.install / folder)
        self._publish()
        settings_save.render_plugin(places, self._settings())
        self.warden = FakeWarden(places, self.clock, accept=CODE, github=Deploys(self), on_event=self.on_event)
        self.warden.start()
        return server.App(server.PublicSettings(places), Warden(self.warden.address, lambda: 5.0), self.clock.now)

    def _settings(self) -> settings_module.Settings:
        return settings_module.load(self.places.defaults, self.places.overrides)

    def _publish(self) -> None:
        public.write_settings(self.places, self._settings())

    def on_event(self, kind: str, data: dict[str, Any]) -> None:
        """The orchestrator's part: a free proposal place is filled from the reserve, then with nothing else."""
        if kind == "proposal-slot-free":
            self.later(REFILL_SECONDS, self.refill)
        if kind == "paused":
            self.later(0.3, self.benched)

    def later(self, seconds: float, stand_in: Callable[[Any], None]) -> None:
        timer = threading.Timer(seconds, stand_in, args=(self.places,))
        self.timers.append(timer)
        timer.start()

    def benched(self, places: Any) -> None:
        """The crash watch's part: once the grace is over, each paused session's bench is finished."""
        if self.warden is None or places != self.places:
            return
        ctx = self.warden.doors.context(WARDEN)
        self.clock.skip(ctx.settings().get_int("watch.bench_grace_seconds"))
        crashwatch.finish_benches(ctx, {})

    def refill(self, places: Any) -> None:
        if self.warden is None or places != self.places:
            return
        ctx = self.warden.doors.context(ORCHESTRATOR)
        if self.reserve:
            task, why = self.reserve.pop(0)
            propose.propose(ctx, task, 1, why, "", (FIXTURES / "pictures" / f"task-{task}.svg").read_bytes(), "")
        else:
            propose.propose_none(ctx)

    def switch(self, body: dict[str, Any]) -> None:
        now = self.clock.now()
        scenario = body.pop("scenario", None)
        if scenario == "outage":
            self._outage(now)
        elif scenario in ("near-limit", "stale-plan"):
            plan = store.read_json(self.places.plan)
            if scenario == "near-limit":
                plan["five_hour"]["used_percentage"], plan["seven_day"]["used_percentage"] = 85, 97
            else:
                plan["written_at"] = now - 52 * 60
            store.write_json(self.places.plan, plan)
        elif scenario == "no-goal":
            (self.repo / "GOAL.md").unlink()
            sessions = registry.read(self.places)
            for s in sessions.values():
                if s.state != models.RETIRED:
                    s.state, s.retired, s.ended = models.RETIRED, models.STOPPED, iso(now - 3600)
            registry.write(self.places, sessions)
        if task := body.pop("bump", None):
            path = self.repo / "queue" / "tasks" / f"{task}.md"
            moved = formats.parse_task(path.read_text())
            moved.why += " It changed since."
            path.write_text(formats.dump_task(moved))
        if saved := body.pop("saved", None):
            overrides = {**self._settings().overrides, **saved}
            store.write_text(self.places.overrides, settings_module.overrides_toml(overrides))
            self._publish()
        if "down" in body and self.warden is not None:
            self.warden.stop() if body["down"] else self.warden.start()
        self.switches.update(body)

    def _outage(self, now: float) -> None:
        """Prod failed two probes; the fix the factory started runs as run-t-0031."""
        for back in (120, 60):
            probe = models.Probe(iso(now - back), "prod", False, "", 900, "HTTP 502", counts=True)
            store.append_jsonl(self.places.probes, models.dump(probe))
        card = json.loads((FIXTURES / "home-reserve.json").read_text())["outage_card"]
        picture = (FIXTURES / "pictures" / "task-t-0031.svg").read_bytes()
        name = f"t-0031-{hashlib.sha256(picture).hexdigest()[:12]}.svg"
        (self.repo / "queue" / "pictures" / name).write_bytes(picture)
        day = iso(now)[:10]
        request = models.Request(
            id=f"r-{day}-1043",
            inbox="agents",
            from_role="warden",
            at=iso(now - 120),
            severity="severe",
            where="prod",
            body="Prod returns 502 on every page",
            outcome="task t-0031",
        )
        (self.repo / "queue" / "requests" / "agents" / f"{request.id}.md").write_text(formats.dump_request(request))
        task = models.Task(
            id="t-0031",
            title=card["line"],
            requests=[request.id],
            goal_item="",
            severity="severe",
            surfaces=["api"],
            status="building",
            created=iso(now - 180),
            body="# Prod answers again",
            cause="app/net.py:20",
            slices=1,
            run=1,
            why=card["why"],
            picture=f"pictures/{name}",
        )
        (self.repo / "queue" / "tasks" / "t-0031.md").write_text(formats.dump_task(task))
        sessions = registry.read(self.places)
        sessions["run-t-0031"] = models.Session(
            id="run-t-0031", name="run-t-0031", role="implementer", family="impl", model="claude-sonnet-5-5",
            effort="high", task="t-0031", parent="", worktree="/srv/wt/run-t-0031", state="working",
            started=iso(now - 170), started_by="warden", rung=1, since=iso(now - 170))  # fmt: skip
        registry.write(self.places, sessions)
        outage = {"since": iso(now - 120), "stage": "fixing", "task": "t-0031", "session": "run-t-0031",
                  "bypass_used": True}  # fmt: skip
        store.write_json(self.places.state / "outage.json", outage)


class Handler(server._Handler):
    live: Live

    def _test(self, answer: Any) -> None:
        self._send(server._json(200, answer))

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/__calls":
            self._test(self.live.calls)
            return
        split = urlsplit(self.path)
        if not split.path.startswith("/api/"):
            super().do_GET()
            return
        self.live.calls.append({"method": "GET", "path": split.path, "query": dict(parse_qsl(split.query))})
        fail = self.live.switches.get("fail_path")
        if fail and split.path.startswith(fail):
            self._send(server._json(500, {"ok": False, "error": "The dashboard hit an error building this page."}))
            return
        headers = {k.lower(): v for k, v in self.headers.items()}
        self._guarded(lambda: self._injected(self.live.app.get(self.path, headers)))

    def _injected(self, reply: server.Reply) -> server.Reply:
        """A part that fails to build, and a newer API, as the dashboard would answer them."""
        sw = self.live.switches
        if reply.headers.get("Content-Type") != "application/json" or not (sw["fail_part"] or sw.get("api")):
            return reply
        envelope = json.loads(reply.body)
        for name in sw["fail_part"]:
            if name in envelope.get("parts", {}):
                envelope["parts"][name] = {"ok": False, "error": f"The {name} could not be read: its file is missing."}
        if sw.get("api"):
            envelope["api"] = sw["api"]
        return server._json(reply.status, envelope)

    def do_POST(self) -> None:  # noqa: N802
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path in ("/__reset", "/__switch"):
            if self.path == "/__reset":
                self.live.app = self.live.fresh()
            else:
                self.live.switch(json.loads(raw or b"{}"))
            self._test({"ok": True})
            return
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = None
        headers = {k.lower(): v for k, v in self.headers.items()}
        self.live.calls.append({"method": "POST", "path": self.path, "headers": headers, "body": body})
        refused = [why for prefix, why in self.live.switches["refuse"].items() if self.path.startswith(prefix)]
        if refused:
            self._send(server._json(409, {"ok": False, "refused": "switch", "reason": refused[0]}))
            return
        self._guarded(lambda: self.live.app.post(self.path, headers, raw))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    port = parser.parse_args().port
    live = Live()
    signal.signal(signal.SIGTERM, live.remove)
    Handler.live = live
    Handler.timeout = 10
    httpd = server._PooledServer(("127.0.0.1", port), live.app, 8)
    httpd.RequestHandlerClass = Handler
    print(f"listening on http://127.0.0.1:{httpd.server_address[1]}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
