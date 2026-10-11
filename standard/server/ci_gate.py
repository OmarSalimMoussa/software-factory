"""The ci user's forced command: the one thing CI's SSH key may do is start a deploy.

sshd runs this for every connection of that key and puts what the client asked for in SSH_ORIGINAL_COMMAND. Only
three shapes pass; anything else is logged to the system log and refused (exit 2). A deploy that passes replaces
this process with the deploy command, run as the admin user, so the registry token on standard input reaches it
untouched.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

from standard import config as config_mod
from standard import environment
from standard.config import ConfigError

PRODUCT_TOML = Path("/etc/software-factory/product.toml")
DEPLOY = "/usr/local/bin/factory-deploy"
SUDO = "/usr/bin/sudo"
TAG = "factory-ci-gate"
_COMMIT = r"[0-9a-f]{7,40}"
_REF = r"-|[A-Za-z0-9._/][A-Za-z0-9._/-]{0,99}"
# fullmatch with explicit classes: a newline, a semicolon or a second space can never pass.
_ACCEPTED = (
    re.compile(rf"deploy (staging) ({_COMMIT}) (follow)"),
    re.compile(rf"deploy (staging) ({_COMMIT}) (pin) ({_REF})"),
    re.compile(rf"deploy (prod) ({_COMMIT})"),
)

Exec = Callable[[str, list[str]], NoReturn]
Note = Callable[[str], None]


def deploy_args(command: str) -> list[str] | None:
    """The deploy command's arguments, or None when ``command`` is not one this gate lets through."""
    for shape in _ACCEPTED:
        match = shape.fullmatch(command)
        if match is not None:
            return list(match.groups())
    return None


def system_log(message: str) -> None:
    subprocess.run(["logger", "-t", TAG, "--", message], check=False, stdin=subprocess.DEVNULL)


def main(
    command: str | None,
    product_toml: Path = PRODUCT_TOML,
    execv: Exec = os.execv,
    note: Note = system_log,
) -> int:
    args = None if command is None else deploy_args(command)
    if args is None:
        # repr() keeps a newline or a control byte from forging a second log line.
        note("refused: a login with no command" if command is None else f"refused: {command[:300]!r}")
        print(f"{TAG}: refused", file=sys.stderr)
        return 2
    try:
        admin = config_mod.parse(product_toml).server.admin
    except ConfigError as exc:
        note(f"cannot read {product_toml}: {len(exc.problems)} problems")
        print(f"{TAG}: the server's settings are unusable; run the server set-up again", file=sys.stderr)
        return 1
    execv(SUDO, ["sudo", "-n", "-u", admin, DEPLOY, *args])


if __name__ == "__main__":
    sys.exit(main(environment.ssh_original_command()))
