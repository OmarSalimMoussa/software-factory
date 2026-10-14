"""Deploy shape: the product can be built, smoke-tested and deployed the standard way.

factory.toml loads and names a ghcr.io registry; the two workflows are the factory's templates, byte for byte (read
from the factory this check runs in, so a product is held to that commit's copy); ``.github/factory-ref`` pins the
factory; every hook factory.toml names is tracked and executable; the build plan can be made, and each image's ignore
file leaves out .claude folders; every built service runs the image CI builds for the commit; the smoke test reaches
built services; the deploy files are tracked and hold compose.yml, the env template and every relative bind mount;
every product image a tracked file names is built; every [env.set] key is in the env template; and every published
port binds 127.0.0.1.
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path
from typing import Any

from checks.runner.repo import Repo
from runtime.shared.paths import PROMOTE_WORKFLOW
from standard import compose as compose_mod
from standard import config as config_mod
from standard import plan as plan_mod
from standard.compose import Compose, ComposeError
from standard.config import FACTORY_REF, FACTORY_REF_LINE, ConfigError
from standard.config_records import Config

ID = "deploy-shape"
RULE = "The product builds, smoke-tests and deploys the standard way, as factory.toml describes"
VALUES_FILE = None
VALUES = None

TEMPLATES = Path(__file__).resolve().parents[2] / "templates" / "ci"
WORKFLOWS = ("ci.yml", PROMOTE_WORKFLOW)
# CI signs in to this registry with the workflow's own GITHUB_TOKEN; it holds no other registry's password.
REGISTRY = "ghcr.io/"
LOOPBACK = "127.0.0.1"
_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
LEFT_OUT = "**/.claude"
_WILDCARD = re.compile(r"[*?\[]")


def _first_difference(mine: bytes, template: bytes) -> int:
    ours, theirs = mine.splitlines(keepends=True), template.splitlines(keepends=True)
    pairs = enumerate(zip(ours, theirs, strict=False), 1)
    return next((n for n, (a, b) in pairs if a != b), min(len(ours), len(theirs)) + 1)


def workflow_problems(repo: Repo) -> list[str]:
    out = []
    for name in WORKFLOWS:
        rel = f".github/workflows/{name}"
        if rel not in repo.tracked:
            out.append(f"{rel} is missing (or not committed): copy the factory's templates/ci/{name} there unchanged")
            continue
        mine, template = (repo.root / rel).read_bytes(), (TEMPLATES / name).read_bytes()
        if mine != template:
            line = _first_difference(mine, template)
            out.append(f"{rel} differs from the factory's template at line {line}: copy templates/ci/{name} over it "
                       "unchanged; the product's settings go in factory.toml")  # fmt: skip
    return out


def ref_problems(repo: Repo) -> list[str]:
    wanted = "one line, owner/name@<40-character sha>, naming the factory repo and commit CI checks with"
    if FACTORY_REF not in repo.tracked:
        return [f"{FACTORY_REF} is missing (or not committed): write {wanted}"]
    lines = repo.read(FACTORY_REF).splitlines()
    if len(lines) != 1 or not FACTORY_REF_LINE.fullmatch(lines[0]):
        return [f"{FACTORY_REF} must hold {wanted}; it holds {repo.read(FACTORY_REF)[:80]!r}"]
    return []


def _under(rel: str, entry: str) -> bool:
    entry = entry.rstrip("/")
    return rel == entry or rel.startswith(f"{entry}/")


def hook_problems(repo: Repo, config: Config) -> list[str]:
    out = []
    for rel in config.hooks():
        if rel not in repo.tracked:
            out.append(f"factory.toml names {rel} as a hook, but it is missing (or not committed): add it")
        elif not (repo.root / rel).stat().st_mode & 0o111:
            out.append(f"factory.toml names {rel} as a hook, but it is not executable: chmod +x it and commit the mode")
    return out


def image_problems(repo: Repo, config: Config, compose: Compose) -> list[str]:
    out: list[str] = []
    try:
        images = plan_mod.images(repo.root, config, compose, "0" * plan_mod.SHORT)
    # telemetry: surfaced — the build plan's problems go into `out`, which the check reports
    except plan_mod.PlanError as exc:
        out += [f"the build plan cannot be made: {p}" for p in exc.problems]
    else:
        out += ignore_problems(repo, images)
    # The env file names the commit's tag; on the server it is the tag of the images CI pushed.
    tags = [key for key, value in config.env.set if value == "{commit}"]
    for service in compose.built():
        ref = plan_mod.image_ref(config, service.name)
        image = service.image or ""
        if not (tags and image.startswith(f"{ref}:") and re.fullmatch(plan_mod.reference(tags), image[len(ref) + 1 :])):
            out.append(f"{compose_mod.FILE}: service {service.name} builds an image, so its image must be "
                       f"{ref}:${{KEY}} with KEY = \"{{commit}}\" in [env.set]: a deploy runs the image CI built for "
                       "the commit")  # fmt: skip
    built = {s.name for s in compose.built()}
    smoke = [config.smoke.health.service, *(e.service for e in config.smoke.expect)]
    out += [f"[smoke] names the service {name}, which compose.yml does not build: smoke-test a built service"
            for name in dict.fromkeys(smoke) if name not in built]  # fmt: skip
    return out


def _named_images(repo: Repo, config: Config, built: set[str]) -> list[str]:
    prefix = plan_mod.image_ref(config, "")
    pattern = re.compile(re.escape(prefix) + r"([a-z0-9][a-z0-9-]*)")
    out = []
    for rel in repo.tracked:
        try:
            text = (repo.root / rel).read_text(encoding="utf-8")
        # telemetry: expected — a binary file is not text, so it names no image to run
        except UnicodeDecodeError:
            continue
        for name in dict.fromkeys(pattern.findall(text)):
            # A name ending in - is a prefix the code appends a key to; some built image must carry it.
            covered = any(b.startswith(name) for b in built) if name.endswith("-") else name in built
            if not covered:
                out.append(f"{rel} names the image {prefix}{name}, which CI never builds, so no deploy can pull it: "
                           "build it (a compose service with a build section, or [build].extra_images)")  # fmt: skip
    return out


def file_problems(repo: Repo, config: Config, compose: Compose) -> list[str]:
    out = []
    for rel in config.deploy.files:
        if not any(_under(t, rel) for t in repo.tracked):
            out.append(f"[deploy].files names {rel}, which is not tracked: commit it or take it off the list")
    for needed in (compose_mod.FILE, config.env.template):
        if not any(_under(needed, entry) for entry in config.deploy.files):
            out.append(f"[deploy].files lacks {needed}, which every deploy reads: add it")
    for service in compose.services.values():
        for source in service.binds:
            if not any(_under(source, entry) for entry in config.deploy.files):
                out.append(f"{compose_mod.FILE}: service {service.name} mounts ./{source}, which a deploy does not "
                           "copy to the server: add it to [deploy].files")  # fmt: skip
    return out


def env_problems(repo: Repo, config: Config) -> list[str]:
    template = config.env.template
    if template not in repo.tracked:
        return [f"[env].template names {template}, which is missing (or not committed)"]
    keys = {m.group(1) for line in repo.read(template).splitlines() if (m := _ENV_LINE.match(line))}
    return [f"[env.set] writes {key}, which {template} does not list: add a {key}= line to it"
            for key, _ in config.env.set if key not in keys]  # fmt: skip


def ignore_problems(repo: Repo, images: tuple[plan_mod.Image, ...]) -> list[str]:
    """Each image's ignore file: the Dockerfile's own, else its context's, as Docker picks it."""
    built: dict[str, list[str]] = {}
    for image in images:
        own = f"{image.dockerfile}.dockerignore"
        ignore = own if own in repo.tracked else posixpath.normpath(posixpath.join(image.context, ".dockerignore"))
        built.setdefault(ignore, []).append(image.name)
    out = []
    for ignore, names in built.items():
        fails = (
            f"so the build of {', '.join(names)} reads the agents' .claude folders and fails on the ones it cannot read"
        )
        if ignore not in repo.tracked:
            out.append(f"{ignore} is missing (or not committed), {fails}: add it with the line {LEFT_OUT}")
            continue
        lines = [line.strip() for line in repo.read(ignore).splitlines()]
        if LEFT_OUT not in {line.rstrip("/") for line in lines}:
            out.append(f"{ignore} does not leave out {LEFT_OUT}, {fails}: add the line {LEFT_OUT}")
        out += [
            f"{ignore} lets {line} back in, and a wildcard or .claude there makes Docker look inside .claude folders, "
            f"{fails}: name each file or folder to keep in full"
            for line in lines
            if line.startswith("!") and (_WILDCARD.search(line) or ".claude" in line)
        ]
    return out


def port_problems(compose: Compose) -> list[str]:
    return [f"{compose_mod.FILE}: service {service.name} publishes {port.text!r} beyond this machine: bind it to "
            f"{LOOPBACK} ({LOOPBACK}:<port>:<target>); the edge is the only way in"
            for service in compose.services.values() for port in service.ports if port.host_ip != LOOPBACK]  # fmt: skip


def run(repo: Repo, values: dict[str, Any]) -> list[str]:
    out = workflow_problems(repo) + ref_problems(repo)
    try:
        config = config_mod.load(repo.root)
    # telemetry: surfaced — factory.toml's problems go into `out`, which the check reports
    except ConfigError as exc:
        return out + [f"{config_mod.FILE}: {p}" for p in exc.problems]
    if not config.product.registry.startswith(REGISTRY):
        out.append(f"[product].registry is {config.product.registry}, but CI pushes only to {REGISTRY}<owner>, where "
                   "it signs in with the workflow's own token: set it to ghcr.io/<owner>")  # fmt: skip
    out += hook_problems(repo, config) + env_problems(repo, config)
    if compose_mod.FILE not in repo.tracked:
        return [*out, f"{compose_mod.FILE} is missing (or not committed): the stack every deploy runs"]
    try:
        compose = compose_mod.read(repo.root / compose_mod.FILE)
    # telemetry: surfaced — the compose file's problems go into `out`, which the check reports
    except ComposeError as exc:
        return out + [f"{compose_mod.FILE}: {p}" for p in exc.problems]
    built = {s.name for s in compose.built()} | {e.name for e in config.build.extra_images} | {plan_mod.DEPLOY_IMAGE}
    out += image_problems(repo, config, compose) + file_problems(repo, config, compose)
    return out + _named_images(repo, config, built) + port_problems(compose)
