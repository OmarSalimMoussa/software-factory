"""Whether a command the stack unit runs for an agent would land in a container that commands this machine's Docker.

A container commands Docker when it mounts Docker's socket, or a folder that holds it, or has the socket where Docker
keeps it, or runs privileged. Any command in it then commands this machine, and prod beside the slots: not only
`docker`, but python or curl on the socket. So `run` is refused for a service when any of the slot's containers of it
does. The slot's containers are those with the slot label the product gives everything a slot makes, and a
service's those with the label compose gives each container of it. A run's new container mounts what the service's
container from the last `up` mounts, so a service with no container yet is refused: what it would mount is unknown.
So is one docker cannot say about. Only the mounts' paths and the privileged flag are read, never a container's
environment, which holds its secrets.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import PurePosixPath

from runtime.system.runners.same_up import Docker
from standard.services import host_paths

COMPOSE_SERVICE = "com.docker.compose.service"
_TOLD = (
    "[{{json .Id}},{{json .Name}},{{json .HostConfig.Privileged}}"
    "{{range .Mounts}},{{json .Source}},{{json .Destination}}{{end}}]"
)


def _commands(told: list[object]) -> bool:
    """Whether one container, as ``_TOLD`` prints it, commands Docker."""
    sockets = {host_paths.DOCKER_SOCKET, os.path.realpath(host_paths.DOCKER_SOCKET)}
    sources, destinations = [str(p) for p in told[3::2]], told[4::2]
    held = any(PurePosixPath(socket).is_relative_to(source) for socket in sockets for source in sources)
    return told[2] is True or held or any(path in sockets for path in destinations)


def run_refusal(label: str, service: str, docker: Docker, timeout: int) -> str:
    """Why a run of ``service`` may not start, "" when it may; ``label`` is the slot's, as ``key=value``."""
    own = ["--filter", f"label={label}", "--filter", f"label={COMPOSE_SERVICE}={service}"]
    try:
        found = docker(["container", "ls", "-aq", *own], timeout)
        ids = found.stdout.split()
        told = docker(["container", "inspect", "--format", _TOLD, *ids], timeout) if ids else found
        rows = [json.loads(line) for line in told.stdout.splitlines() if line.strip()] if ids else []
    # telemetry: surfaced — `exc` goes into the refusal, which the caller prints to the run's log
    except (subprocess.TimeoutExpired, ValueError) as exc:
        return f"docker could not say what {service}'s containers mount ({type(exc).__name__}); try again"
    if found.returncode != 0 or told.returncode != 0 or len(rows) != len(ids):
        return f"docker could not say what {service}'s containers mount: {(found.stderr + told.stderr).strip()[-200:]}"
    if not ids:
        return f"it has no {service} container yet, so what a run of it would mount is unknown: bring it up first"
    if any(_commands(row) for row in rows):
        return (f"{service}'s container commands this machine's Docker (it mounts Docker's socket or runs privileged), "
                "so any command in it, docker or not, would command this machine and prod beside the slots. Read "
                "what you need in your worktree or through another service; what needs Docker itself is the owner's: "
                "say so in your report")  # fmt: skip
    return ""
