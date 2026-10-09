"""Deploy bundle: everything the stack mounts is in the deploy bundle an image carries.

A deploy that runs the stack from a bundle an image copies in, not from a checkout, fails on the host once a file the
compose file bind-mounts is missing from it. For each bundle: every relative path a compose service mounts (``./x``,
short or long volume syntax), and every listed required path, must sit under a ``COPY <src>... <bundle dir>/...``
source of the listed Dockerfile (``--from`` copies excluded). A host singleton (a ``container_name`` that does not
carry the workspace variable) takes no ``env_file``: a deploy that rewrites the env file would restart it.
"""

from __future__ import annotations

import re
from typing import Any

from checks.runner.repo import Repo
from checks.standard.infra import configs

ID = "deploy-bundle"
RULE = "Everything the stack mounts is in the deploy bundle"
VALUES_FILE = "deploy-bundle.toml"
VALUES = {
    "bundles": [{"compose": str, "dockerfile": str, "bundle_dir": str, "required": [str], "workspace_variable": str}]
}


def bundle_sources(dockerfile_text: str, bundle_dir: str) -> list[str]:
    copy = re.compile(r"^COPY\s+(?!--from)(.+?)\s+" + re.escape(bundle_dir.rstrip("/")) + r"(?:/\S*)?\s*$")
    sources: list[str] = []
    for line in dockerfile_text.splitlines():
        match = copy.match(line)
        if match:
            sources += [source.rstrip("/").removeprefix("./") for source in match.group(1).split()
                        if not source.startswith("--")]  # fmt: skip
    return sources


def mounted_paths(services: dict[str, dict[str, Any]]) -> list[str]:
    found: list[str] = []
    for service in services.values():
        for volume in service.get("volumes") or []:
            source = str(volume.get("source", "")) if isinstance(volume, dict) else str(volume).split(":", 1)[0]
            if source.startswith("./") and source[2:].rstrip("/") not in found:
                found.append(source[2:].rstrip("/"))
    return found


def _covered(path: str, sources: list[str]) -> bool:
    return any(source in (".", "") or path == source or path.startswith(f"{source}/") for source in sources)


def _bundle_problems(repo: Repo, bundle: dict[str, Any]) -> list[str]:
    compose, dockerfile = bundle["compose"], bundle["dockerfile"]
    try:
        services = configs.services(repo, compose)
    except configs.ConfigError as exc:
        return [f"{ID}: {exc}"]
    if not (repo.root / dockerfile).is_file():
        return [f"{ID}: {dockerfile} is missing"]
    sources = bundle_sources(repo.read(dockerfile), bundle["bundle_dir"])
    remedy = f"add a `COPY <path> {bundle['bundle_dir']}/<path>` line to {dockerfile}"
    problems = [f"{ID}: {compose} mounts ./{path} but the deploy bundle lacks it; {remedy}"
                for path in mounted_paths(services) if not _covered(path, sources)]  # fmt: skip
    problems += [f"{ID}: the deploy bundle lacks {path}, which the deploy runs; {remedy}"
                 for path in bundle["required"] if not _covered(path.rstrip("/"), sources)]  # fmt: skip
    marker = "${" + bundle["workspace_variable"]
    for name, service in services.items():
        container = service.get("container_name")
        if container is not None and marker not in str(container) and service.get("env_file"):
            problems.append(f"{ID}: host singleton {name} reads an env_file, so every deploy that rewrites it "
                            "restarts the service; drop the env_file and name each key under `environment:` as "
                            "`KEY: ${KEY:?}`")  # fmt: skip
    return problems


def run(repo: Repo, values: dict[str, Any]) -> list[str]:
    return [problem for bundle in values["bundles"] for problem in _bundle_problems(repo, bundle)]
