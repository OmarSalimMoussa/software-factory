"""Whether a command the stack unit runs for an agent would land in a container that commands this machine's Docker.

A container commands Docker when it mounts Docker's socket, or a folder that holds it, or has the socket where Docker
keeps it, or runs privileged. Any command in it then commands this machine, and prod beside the slots: not only
`docker`, but python or curl on the socket. So `run` is refused for a service when any of the slot's containers of it
does, and `exec` runs only in a container of the slot that does not, such as a browser the slot's stack started. The
slot's containers are those with the slot label the product gives everything a slot makes, and a service's those
with the label compose gives each container of it. A run's new container mounts what the service's container from
the last `up` mounts, so a service with no container yet is refused: what it would mount is unknown. So is one docker
cannot say about. Only the mounts' paths and the privileged flag are read, never a container's environment, which
holds its secrets. Docker does not stop what an exec started when the exec's time runs out: the answer says so.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

from runtime.shared import environment
from runtime.system.runners import launch_file as lf
from runtime.system.runners.same_up import Docker
from standard.services import host_paths

COMPOSE_SERVICE = "com.docker.compose.service"
_TOLD = (
    "[{{json .Id}},{{json .Name}},{{json .HostConfig.Privileged}}"
    "{{range .Mounts}},{{json .Source}},{{json .Destination}}{{end}}]"
)
_COMMANDS = "commands this machine's Docker (it mounts Docker's socket or runs privileged), so any command in it"


class Unknown(Exception):
    """docker could not say what the slot's containers mount."""


def _told(filters: list[str], docker: Docker, timeout: int) -> list[list[Any]]:
    """Each container the filters pick, as ``_TOLD`` prints it."""
    try:
        found = docker(["container", "ls", "-aq", *filters], timeout)
        ids = found.stdout.split()
        told = docker(["container", "inspect", "--format", _TOLD, *ids], timeout) if ids else found
        rows = [json.loads(line) for line in told.stdout.splitlines() if line.strip()] if ids else []
    except (subprocess.TimeoutExpired, ValueError) as exc:
        raise Unknown(type(exc).__name__) from exc
    if found.returncode != 0 or told.returncode != 0 or len(rows) != len(ids):
        raise Unknown((found.stderr + told.stderr).strip()[-200:])
    return rows


def _commands(told: list[Any]) -> bool:
    sockets = {host_paths.DOCKER_SOCKET, os.path.realpath(host_paths.DOCKER_SOCKET)}
    sources, destinations = [str(p) for p in told[3::2]], told[4::2]
    held = any(PurePosixPath(socket).is_relative_to(source) for socket in sockets for source in sources)
    return told[2] is True or held or any(path in sockets for path in destinations)


def run_refusal(label: str, service: str, docker: Docker, timeout: int) -> str:
    """Why a run of ``service`` may not start, "" when it may; ``label`` is the slot's, as ``key=value``."""
    try:
        rows = _told(["--filter", f"label={label}", "--filter", f"label={COMPOSE_SERVICE}={service}"], docker, timeout)
    # telemetry: surfaced — `exc` goes into the refusal, which the caller prints to the run's log
    except Unknown as exc:
        return f"docker could not say what {service}'s containers mount: {exc}"
    if not rows:
        return f"it has no {service} container yet, so what a run of it would mount is unknown: bring it up first"
    if any(_commands(row) for row in rows):
        return (f"{service}'s container {_COMMANDS}, docker or not, would command this machine and prod beside the "
                "slots. Read what you need in your worktree, through another service, or in a container the slot "
                "started (factory slot exec); what needs Docker itself is the owner's: say so in your "
                "report")  # fmt: skip
    return ""


def exec_in(label: str, words: list[str], docker: Docker, timeout: int) -> int:
    """`exec` as ``<container> -- <command…>`` in a container with the slot's ``label``; the command's exit."""
    name, command = words[0] if words else "", words[2:]
    if words[1:2] != ["--"] or not command:
        print("stack-run: ran nothing: exec <container> -- <command>", flush=True)
        return lf.REFUSED
    try:
        found = [row for row in _told(["--filter", f"label={label}"], docker, timeout) if row[1] == f"/{name}"]
    # telemetry: surfaced — `print`ed to the run's log, and the exit says it ran nothing
    except Unknown as exc:
        print(f"stack-run: ran nothing: docker could not say what this slot's containers mount: {exc}", flush=True)
        return lf.REFUSED
    if not found:
        why = f"this slot has no container named {name}; factory slot top names them"
    else:
        why = f"{name} {_COMMANDS} would command this machine and prod beside the slots" if _commands(found[0]) else ""
    if why:
        print(f"stack-run: ran nothing: {why}", flush=True)
        return lf.REFUSED
    status = lf.run(["docker", "exec", found[0][0], *command], Path("/"), environment.handed_on(), timeout)
    if status == lf.TIMED_OUT:
        print(f"stack-run: the command may still run in {name}: docker does not stop it", flush=True)
    return status
