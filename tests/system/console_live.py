"""The install console on a temporary machine, for its browser drive. It changes nothing anywhere: every action runs
with a dry host that reads only the facts below, and an action that would change the machine is refused.

Run: python3 tests/system/console_live.py. It prints "listening on <link>" once ready; the link works once.
"""

from __future__ import annotations

import getpass
import os
import shutil
import signal
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parents[1]), str(HERE)]

from system_fakes import FACTORY_ORIGIN, FIXTURE, PASSING, PRODUCT_ORIGIN, ci_answers  # noqa: E402

from runtime.shared.paths import Places  # noqa: E402
from runtime.system.console import serve  # noqa: E402
from runtime.system.console_app import Choices, Console, Hosts  # noqa: E402
from runtime.system.console_run import ConsoleDryHost, Run  # noqa: E402
from runtime.system.host import Account, DryHost, Host  # noqa: E402
from runtime.system.job import REPO_ROOT  # noqa: E402

NEWEST = "0123456789abcdef0123456789abcdef01234567"


def refuse_real(run: Run) -> Host:
    raise RuntimeError("the drive's console runs nothing for real")


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="console-live-"))
    product = root / "shop"
    product.mkdir()
    shutil.copy(FIXTURE / "factory.toml", product / "factory.toml")
    answers = {
        f"git -C {REPO_ROOT} remote get-url origin": (0, FACTORY_ORIGIN),
        f"git -C {product} remote get-url origin": (0, PRODUCT_ORIGIN),
    }
    git = f"git -C {REPO_ROOT}"
    reader = DryHost(answers={
        f"{git} symbolic-ref --short refs/remotes/origin/HEAD": (0, "origin/main\n"),
        f"{git} log -1 --format=%H%n%h%n%s main --": (0, f"{NEWEST}\n0123456\nThe drive's newest commit\n"),
        **ci_answers(REPO_ROOT, NEWEST, PASSING),
    })  # fmt: skip
    owner = Account(getpass.getuser(), os.getuid(), os.getgid(), root / "home", ("owner",))
    opened: list[str] = []

    def dry(run: Run) -> Host:
        return ConsoleDryHost(run, dict(answers))

    def open_for_owner(target: str) -> bool:
        opened.append(target)
        return True

    console = Console(Places.under(root / "machine"), owner, reader, Hosts(refuse_real, dry), open_for_owner,
                      commit=None, repo=product, source=None, logs=root / "logs", log_group="",
                      choices=Choices())  # fmt: skip
    server = serve(console, 10)

    def stop(*_: Any) -> None:
        console.stop()
        threading.Thread(target=server.shutdown, daemon=True).start()
        shutil.rmtree(root, ignore_errors=True)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    print(f"listening on {console.link()}", flush=True)
    console.stopped.wait()
    stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
