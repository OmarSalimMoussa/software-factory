"""The warden's service: the composition root, its sockets, and the pass it runs every ``watch.tick_seconds``.

Settings are checked at start, and the start stops with every problem listed. Each pass runs the loop (vitals,
probes, outage, inbox events), the session watch (backstop, benches, crashes), cost, plan limits, reaping and the
nightly prune, in that order. A pass that fails is logged with its class and the next one still runs; systemd
restarts the warden if the process itself dies.
"""

from __future__ import annotations

import logging
import pwd
import socket
import threading
import time
from collections.abc import Callable, Sequence
from functools import partial
from pathlib import Path

from runtime.qa.replay import pending
from runtime.queue import reports, turn
from runtime.shared import environment, error_reports, store
from runtime.shared import settings as settings_module
from runtime.shared.paths import AGENT_USER, DASHBOARD_USER, Places
from runtime.shared.settings import Settings
from runtime.warden import gc, imports, inbox, loop, public, schedule, server, upgrade
from runtime.warden.context import Caller, CheckPlaces, Clock, Context
from runtime.warden.delivery import ci_watch, done, github, github_push, prod_push
from runtime.warden.dispatch import Dispatcher, Doors, resolve
from runtime.warden.owner import ops, reflections, settings_save, yolo
from runtime.warden.sessions import crashwatch, launch, reap, registry
from runtime.warden.spend import cost, plan
from runtime.warden.verbs import VERBS

log = logging.getLogger("factory.warden")

WARDEN = Caller(kind="warden", role="warden")
Pass = Callable[[Context], object]


class FreshSettings:
    """The effective settings, loaded again whenever the defaults or the overrides file is replaced."""

    def __init__(self, places: Places) -> None:
        self.places = places
        self._lock = threading.Lock()
        self._key: tuple[object, object] | None = None
        self._settings: Settings | None = None

    @staticmethod
    def _stamp(path: Path) -> tuple[int, int, int] | None:
        try:
            info = path.stat()
        # telemetry: expected — no overrides file until the owner's first save
        except FileNotFoundError:
            return None
        return info.st_ino, info.st_mtime_ns, info.st_size

    def __call__(self) -> Settings:
        key = (self._stamp(self.places.defaults), self._stamp(self.places.overrides))
        with self._lock:
            if self._settings is None or key != self._key:
                self._settings = settings_module.load(self.places.defaults, self.places.overrides)
                self._key = key
            return self._settings


def run_passes(ctx: Context, passes: Sequence[tuple[str, Pass]]) -> None:
    for name, run in passes:
        loop.run_once(ctx, f"the {name} pass", partial(run, ctx))


def _ask_branch(ctx: Context) -> None:
    # In its own thread: a fetch can take minutes, and a failure is reported there, once while it lasts.
    loop.run_once(ctx, "the branch ask", lambda: prod_push.ask_branch(ctx))


def _watch_ci(ctx: Context) -> None:
    # In its own thread: a failed run's request reads its jobs' logs from GitHub.
    loop.run_once(ctx, "the CI watch", lambda: ci_watch.poll(ctx))


def _offer_push(ctx: Context) -> None:
    # In its own thread: a look fetches from GitHub and the agents' checkout.
    loop.run_once(ctx, "the staging push's offer", lambda: github_push.offer(ctx))


def _refresh_done(ctx: Context) -> None:
    # In its own thread: it waits for a push to end, and runs slot 0's health script.
    loop.run_once(ctx, "the done list", lambda: done.refresh(ctx))


def _build_replays(ctx: Context, transcripts: Sequence[Path]) -> None:
    # In its own thread: a replay reads a trace of thousands of frames.
    loop.run_once(ctx, "the replays build", lambda: pending.build_pending(ctx, transcripts))


def _pull_reports(ctx: Context) -> None:
    # In its own thread: a failure is reported there, once while it lasts, as a pass's is.
    loop.run_once(ctx, "the reports pull", lambda: reports.poll(ctx))


def passes(places: Places, transcripts: Sequence[Path], agent_home: Path) -> list[tuple[str, Pass]]:
    watch = plan.PlanWatch(plan.source(agent_home, places))
    reaper, nightly = reap.Reaper(), gc.Nightly()
    return [
        ("loop", loop.run_tick),
        ("sessions", crashwatch.SessionsWatch(imports.in_thread("benches")).run_pass),
        # It runs the product's scripts on a slot, for up to slots.verb_minutes: never inside the tick.
        ("reports", imports.Background(_pull_reports, imports.in_thread("reports")).run_pass),
        ("branch", imports.Background(_ask_branch, imports.in_thread("branch")).run_pass),
        ("offer", imports.Background(_offer_push, imports.in_thread("offer")).run_pass),
        ("ci", imports.Background(_watch_ci, imports.in_thread("ci")).run_pass),
        ("done", imports.Background(_refresh_done, imports.in_thread("done")).run_pass),
        (
            "replays",
            imports.Background(partial(_build_replays, transcripts=transcripts), imports.in_thread("replays")).run_pass,
        ),  # fmt: skip
        ("holds", turn.release_held),
        ("schedule", schedule.run_pass),
        ("yolo", yolo.run_pass),
        ("cost", lambda ctx: cost.run_pass(ctx, transcripts)),
        ("plan", watch.run_pass),
        ("reap", reaper.run_pass),
        ("gc", nightly.run_pass),
    ]


def _owner(places: Places) -> pwd.struct_passwd:
    raw = store.read_json(places.owner)
    if not isinstance(raw, dict) or not isinstance(raw.get("user"), str):
        raise store.StoreError(f"{places.owner}: needs the owner's login as user")
    return pwd.getpwnam(raw["user"])


def ready(target: str | None) -> None:
    """Tell systemd the warden serves (Type=notify) on ``target``, its notification socket, once public/ holds what the
    dashboard reads at its start: the dashboard, ordered after the warden, starts no sooner. Outside systemd there is
    no socket and no one to tell."""
    if not target:
        return
    address = "\0" + target[1:] if target.startswith("@") else target  # @ names a socket in the abstract namespace
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.sendto(b"READY=1", address)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    places = Places.system()
    settings = FreshSettings(places)
    current = settings()
    try:
        error_reports.install(places, timeout=current.get_int("watch.http_timeout_seconds"))
    except (error_reports.DsnError, OSError) as exc:
        log.critical("warden: it does not start without the factory's GlitchTip project: %s", exc)
        return 2
    clock = Clock()
    owner, agent_home = _owner(places), launch.agent_home()
    doors = Doors(places, settings, clock, launch.Units(places, settings),
                  github.RestGitHub(places, settings, github.transport), inbox.poster(places, clock),
                  threading.RLock(), CheckPlaces())  # fmt: skip
    dispatcher = Dispatcher(doors, resolve(VERBS))
    ops.recover(places, clock)
    launch.forget_requests(places)
    public.write_settings(places, current)
    public.write_switches(places, public.read_switches(places))
    # The unit runners refuse every request until public/sessions.json exists, the first start's git call included.
    registry.write(places, registry.read(places))
    upgrade.migrate(doors.context(WARDEN))  # before any request or pass reads the state an earlier factory left
    settings_save.render_plugin(places, current)
    peers = server.Peers(
        agent_uid=pwd.getpwnam(AGENT_USER).pw_uid,
        owner_uid=owner.pw_uid,
        dashboard_uid=pwd.getpwnam(DASHBOARD_USER).pw_uid,
        sessions_folder=agent_home / ".claude" / "sessions",
        proc=Path("/proc"),
    )
    server.serve(places, peers, dispatcher, current.get_int("factory.warden_threads"))
    ctx = doors.context(WARDEN)
    refusals = reflections.RefusalWatch()
    server.serve_refusals(places.refusal_socket, lambda line: refusals.record(ctx, line))
    ready(environment.take_notify_socket())
    transcripts = [agent_home / ".claude" / "projects", Path(owner.pw_dir) / ".claude" / "projects"]
    every = passes(places, transcripts, agent_home)
    log.info("warden: serving; passes %s", ", ".join(name for name, _ in every))
    while True:
        run_passes(ctx, every)
        time.sleep(settings().get_int("watch.tick_seconds"))


if __name__ == "__main__":
    raise SystemExit(main())
