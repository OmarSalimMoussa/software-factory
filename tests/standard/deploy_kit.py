"""Fakes for the server-side tests: a product's settings and repo, a Docker that keeps state, a clock and a health
address."""

from __future__ import annotations

import ast
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from standard.config import parse_text
from standard.config_records import Config

FACTORY_TOML = """
[product]
name = "notes"
registry = "ghcr.io/owner"

[build]
commit_arg = "NOTES_BUILD_COMMIT"
tag_arg = "NOTES_IMAGE_TAG"
extra_images = []

[services]
owner_email = "owner@example.com"
errors = ["app", "web-ui"]
errors_public_path = "/telemetry"

[env]
file = "notes.env"
template = "notes.env.template"

[env.set]
NOTES_WORKSPACE = "{workspace}"
NOTES_ORIGIN = "{origin}"
NOTES_IMAGE_TAG = "{commit}"

[server]
host = "notes.example.com"
host_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINotesFixtureHostKey"
admin = "deploy"
tailnet = "notes-box.example.ts.net"
acme_email = "owner@example.com"

[workspaces.prod]
domain = "notes.example.com"
port = 8300
after_deploy = ["hooks/after.sh"]

[deploy]
health = "/healthz"
answer_seconds = 20
drain_minutes = 1
files = ["compose.yml", "notes.env.template"]
keep_images = 2

[edge]
blocked_paths = ["/admin/export*"]
routes = [{ host = "api.notes.example.com", redirect = "notes.example.com" }]
"""

TEMPLATE = """# The notes app's env file.
NOTES_WORKSPACE=laptop
NOTES_ORIGIN=http://127.0.0.1:8000
NOTES_IMAGE_TAG=dev
# server: the laptop's mail host relays nothing
MAIL_HOST=localhost
SESSION_KEY=generate-with-openssl-rand-hex-16
DATABASE_URL=postgresql://laptop
"""

COMPOSE = """services:
  app:
    image: ghcr.io/owner/notes-app:${NOTES_IMAGE_TAG}
    build:
      context: .
      dockerfile: app/Dockerfile
      args:
        NOTES_BUILD_COMMIT: ${NOTES_IMAGE_TAG:?}
    networks: [shared]
networks:
  shared:
    external: true
    name: ${FACTORY_SERVICES_NETWORK}
"""

SERVICES_ENV = "\n".join(
    [
        "POSTGRES_PASSWORD=pg",
        "GLITCHTIP_OWNER_PASSWORD=owner-secret",
    ]
)


def config(text: str = FACTORY_TOML) -> Config:
    return parse_text(text)


def commit(repo: Path, files: dict[str, str]) -> str:
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
        (repo / name).chmod(0o755 if name.endswith(".sh") else 0o644)

    def git(*argv: str) -> str:
        done = subprocess.run(["git", "-C", str(repo), *argv], check=True, capture_output=True, text=True)
        return done.stdout.strip()

    if not (repo / ".git").exists():
        git("init", "-q")
    git("add", "-A")
    who = ["-c", "user.name=Owner", "-c", "user.email=owner@example.com", "-c", "commit.gpgsign=false"]
    git(*who, "commit", "-q", "--no-verify", "-m", "change")
    return git("rev-parse", "HEAD")


def ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout, "")


@dataclass
class FakeDocker:
    """Answers the docker commands the factory runs, keeping networks and images as Docker would."""

    networks: dict[str, set[str]] = field(default_factory=dict)
    images: list[str] = field(default_factory=list)
    in_use: list[str] = field(default_factory=list)
    calls: list[tuple[list[str], str | None]] = field(default_factory=list)
    project_ids: dict[str, int] = field(default_factory=dict)
    glitchtip_params: list[dict[str, Any]] = field(default_factory=list)
    build_exit: int = 0
    build_seconds: float = 0  # how long each build takes on ``clock``
    clock: FakeClock | None = None

    def __call__(self, argv: list[str], stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, stdin))
        if argv[1] == "build" and self.clock is not None:
            self.clock.at += self.build_seconds
        if argv[1] == "build" and self.build_exit:
            return subprocess.CompletedProcess(
                argv, self.build_exit, "#5 RUN pip install\n", "ERROR: failed to solve\n"
            )
        if argv[1] == "network":
            return self.network(argv)
        if argv[1:4] == ["exec", "-i", "factory-glitchtip"]:
            return ok("noise from Django\n" + self.glitchtip(stdin or ""))
        if argv[1] == "build":
            self.images.append(argv[argv.index("--tag") + 1])
        elif argv[1:3] == ["image", "rm"]:
            self.images.remove(argv[3])
        return ok(self.listed(argv[1]))

    def network(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        verb = argv[2]
        if verb == "ls":
            return ok("\n".join(self.networks) + "\n")
        if verb == "inspect":
            joined = {f"id-{name}": {"Name": name} for name in sorted(self.networks[argv[3]])}
            return ok(json.dumps(joined) + "\n")
        if verb == "create":
            self.networks[argv[3]] = set()
        elif verb == "connect":
            self.networks[argv[3]].add(argv[4])
        elif verb == "disconnect":
            self.networks[argv[3]].discard(argv[4])
        elif verb == "rm":
            del self.networks[argv[3]]
        return ok()

    def listed(self, verb: str) -> str:
        """What docker ps and images print; nothing for the other commands."""
        if verb == "ps":
            return "\n".join(self.in_use) + "\n"
        if verb == "images":
            return "".join(f"{ref}\tsha256:{n}\n" for n, ref in enumerate(self.images))
        return ""

    def glitchtip(self, script: str) -> str:
        line = next(text for text in script.splitlines() if text.startswith("PARAMS = json.loads("))
        params = json.loads(ast.literal_eval(line[len("PARAMS = json.loads(") : -1]))
        self.glitchtip_params.append(params)
        if params["action"] == "bootstrap":
            return 'FACTORY-RESULT {"token": "read-token"}\n'
        dsns = {}
        for name, slug in params["projects"].items():
            number = self.project_ids.setdefault(slug, len(self.project_ids) + 2)
            dsns[name] = f"https://key{number}@notes-box.example.ts.net:8200/{number}"
        return "FACTORY-RESULT " + json.dumps({"dsns": dsns}) + "\n"

    def argvs(self) -> list[list[str]]:
        return [argv for argv, _ in self.calls]


@dataclass
class FakeClock:
    at: float = 0.0
    slept: list[float] = field(default_factory=list)

    def now(self) -> float:
        return self.at

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.at += seconds


@dataclass
class FakeHealth:
    """Each address answers its listed answers in turn, then repeats the last one."""

    answers: dict[str, list[tuple[int, dict[str, object]] | None]]

    def __call__(self, url: str) -> tuple[int, bytes] | None:
        queue = self.answers[url]
        found = queue.pop(0) if len(queue) > 1 else queue[0]
        return None if found is None else (found[0], json.dumps(found[1]).encode())
