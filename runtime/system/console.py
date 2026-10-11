"""The install console: install, reinstall and uninstall the factory from a page in your browser, as root:

    sudo python3 runtime/system/console.py [--commit <sha>] [--repo <product checkout>]

It serves one page on 127.0.0.1 at a free port, opens it in your browser and prints its link. The link works once:
its token becomes a cookie that only that browser holds. The page runs the installer's and the uninstaller's own
steps, one action at a time, and asks in place what the command line asks in the terminal. Ctrl-C or the page's Close
stops it.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import threading
import tomllib
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from runtime.shared.paths import Places  # noqa: E402 - the path line above must run first
from runtime.shared.protocol import Refused  # noqa: E402
from runtime.system import console_page, install  # noqa: E402
from runtime.system.console_app import Console, Hosts, Reply, refused  # noqa: E402
from runtime.system.console_run import ConsoleHost, ConsoleLookingHost  # noqa: E402
from runtime.system.host import (  # noqa: E402
    Account,
    CommandError,  # noqa: E402
    Host,
    RealHost,
    as_user,
)
from runtime.system.job import REPO_ROOT  # noqa: E402

log = logging.getLogger("factory.console")

PROGRAM = "console.py"
POLICY = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)
MAX_BODY = 64 * 1024  # a form or an answer; the page sends nothing larger
_DISPLAY = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY")


# ---- HTTP --------------------------------------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def _answer(self, body: bytes) -> None:
        headers = {k.lower(): v for k, v in self.headers.items()}
        try:
            reply = self.server.console.handle(self.command, self.path, headers, body)
        except Exception as exc:
            log.exception("console: %s %s failed (%s)", self.command, urlsplit(self.path).path, type(exc).__name__)
            reply = refused(500, f"The console failed ({type(exc).__name__}); its terminal has the details.")
        self._send(reply)

    def _send(self, reply: Reply) -> None:
        self.send_response(reply.status)
        sent = {
            "Content-Security-Policy": POLICY,
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store",
            **reply.headers,
            "Content-Length": str(len(reply.body)),
        }
        for name, value in sent.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(reply.body)

    def do_GET(self) -> None:  # noqa: N802 -- the name http.server calls
        self._answer(b"")

    def do_POST(self) -> None:  # noqa: N802
        try:
            size = int(self.headers.get("Content-Length") or 0)
        # telemetry: surfaced — the caller is told the change is not one the console takes
        except ValueError:
            size = -1
        if not 0 <= size <= MAX_BODY:
            self._send(refused(413, "That is not a change the console takes."))
            return
        self._answer(self.rfile.read(size))

    def log_message(self, format: str, *args: Any) -> None:
        """No request is printed: an answer may carry a secret."""


class _Server(ThreadingHTTPServer):
    """Each request on its own thread; closing the server waits for them, so the page's last answer is sent whole."""

    def __init__(self, console: Console) -> None:
        self.console = console
        super().__init__(("127.0.0.1", 0), _Handler)


def serve(console: Console, read_seconds: float) -> ThreadingHTTPServer:
    """Answer on 127.0.0.1 at a free port, which the console then knows; returns the server to shut it down. A caller
    that sends nothing for ``read_seconds`` is dropped."""
    _Handler.timeout = read_seconds
    server = _Server(console)
    console.port = server.server_address[1]
    threading.Thread(target=server.serve_forever, name="console", daemon=True).start()
    return server


# ---- the owner's desktop -----------------------------------------------------------------------------------------


def display(reader: Host, owner: Account, inherited: Mapping[str, str]) -> dict[str, str]:
    """Where the owner's desktop session draws: their user manager's environment holds it; sudo may have kept it."""
    code, out = reader.query(["systemctl", "--user", "show-environment"], user=owner.name)
    found = dict(line.split("=", 1) for line in out.splitlines() if "=" in line) if code == 0 else {}
    session = {key: found[key] for key in _DISPLAY if key in found}
    return session or {key: inherited[key] for key in _DISPLAY if key in inherited}


def opener(owner: Account, drawn: Mapping[str, str]) -> Callable[[str], bool]:
    """Opens a link or a folder as the owner, in their desktop session; False when no session was found."""

    def open_for_owner(target: str) -> bool:
        if not {"DISPLAY", "WAYLAND_DISPLAY"} & drawn.keys():
            return False
        subprocess.Popen(as_user(owner, ["xdg-open", target], drawn), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)  # fmt: skip
        return True

    return open_for_owner


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=PROGRAM, description=__doc__.splitlines()[0])
    parser.add_argument(
        "--commit", help="the factory commit to offer; else the newest on your checkout's default branch"
    )
    parser.add_argument("--repo", type=Path, help="your checkout of the product; else the one the last install named")
    args = parser.parse_args(argv)
    if os.geteuid() != 0 or "SUDO_USER" not in os.environ:
        print(f"{PROGRAM}: run it with sudo, as the owner", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    reader = RealHost()
    owner = reader.account(os.environ["SUDO_USER"])
    if owner is None:
        print(f"{PROGRAM}: the owner's account cannot be read", file=sys.stderr)
        return 2
    if install.STAGE not in os.environ:
        # The whole console runs from the commit it offers, so the steps it runs are that commit's own.
        offered = console_page.commit_choice(reader, owner, REPO_ROOT, args.commit)
        if offered["problem"]:
            print(f"{PROGRAM}: {offered['problem']}", file=sys.stderr)
            return 1
        try:
            sha, stage = install.stage_commit(offered["value"], owner.name)
        except (Refused, CommandError) as exc:
            print(f"{PROGRAM}: {exc.reason if isinstance(exc, Refused) else exc}", file=sys.stderr)
            return 1
        print(f"The console runs {sha[:12]} ({offered['subject']}), archived in {stage}", flush=True)
        install.exec_staged(sha, stage, "runtime/system/console.py", ["--commit", sha, *_without_commit(argv)])
    console = Console(Places.system(), owner, reader, Hosts(ConsoleHost, ConsoleLookingHost),
                      opener(owner, display(reader, owner, os.environ)), commit=args.commit,
                      repo=args.repo.resolve() if args.repo else None)  # fmt: skip
    defaults = tomllib.loads((REPO_ROOT / "settings.toml").read_text(encoding="utf-8"))
    server = serve(console, defaults["watch"]["socket_read_seconds"])
    link = console.link()
    print(f"The install console: {link}", flush=True)
    where = "It is opening in your browser." if console.opener(link) else "Open the link in your browser."
    print(f"{where} The link works once. Press Ctrl-C, or Close on the page, to stop the console.", flush=True)
    try:
        try:
            console.stopped.wait()
        except KeyboardInterrupt:
            print(flush=True)
        console.stop()
    except KeyboardInterrupt:
        print("Stopped without waiting for the action to end.", file=sys.stderr)
        return 130
    finally:
        server.shutdown()
        server.server_close()
    return 0


def _without_commit(argv: list[str]) -> list[str]:
    """``argv`` with its --commit (either spelling) taken out: the staged console is told the full sha instead."""
    out, skip = [], False
    for arg in argv:
        if skip:
            skip = False
        elif arg == "--commit":
            skip = True
        elif not arg.startswith("--commit="):
            out.append(arg)
    return out


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
