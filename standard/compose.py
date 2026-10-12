"""Reading a product's compose.yml and the Dockerfiles it builds, for the build plan, the deploy and the checks.

Only what those read is kept: each service's build section, image, relative bind mounts, published ports, networks,
network mode and whether it has a healthcheck; the top-level networks; and each Dockerfile's FROM lines with the ARG
defaults above them. Values stay as written, ``${VAR}`` and all: the reader that knows the env resolves them.
ComposeError lists every problem at once.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

from standard import yaml_subset

FILE = "compose.yml"


class ComposeError(Exception):
    """compose.yml or a Dockerfile it builds is missing, unreadable or malformed; ``problems`` lists every one."""

    def __init__(self, path: Path, problems: list[str]) -> None:
        self.path = path
        self.problems = problems
        super().__init__(f"{path}: " + "; ".join(problems))


@dataclass(frozen=True, slots=True)
class Build:
    context: str  # as written, relative to the compose file's folder
    dockerfile: str  # as written, relative to the context
    args: tuple[tuple[str, str | None], ...]  # (name, value as written); None takes the value from the environment
    target: str | None


class Port(NamedTuple):
    host_ip: str  # "" when the line binds every interface
    published: str  # "" when Docker picks the host port
    target: str
    text: str  # the entry as written, for messages


class Network(NamedTuple):
    external: bool
    name: str  # the name the file gives it; "" when it gives none and compose names it after the project


@dataclass(frozen=True, slots=True)
class Service:
    name: str
    build: Build | None
    image: str | None
    binds: tuple[str, ...]  # relative bind-mount sources, normalised (``./conf/a.toml`` -> ``conf/a.toml``)
    ports: tuple[Port, ...]
    networks: tuple[str, ...]
    network_mode: str | None
    healthcheck: bool


@dataclass(frozen=True, slots=True)
class Compose:
    path: Path
    services: dict[str, Service]  # in file order
    networks: dict[str, Network]

    def built(self) -> list[Service]:
        """The services with a build section, in file order: each is one of the product's images."""
        return [s for s in self.services.values() if s.build is not None]


@dataclass(frozen=True, slots=True)
class Dockerfile:
    args: dict[str, str | None]  # the ARGs above the first FROM, the only ones a FROM line can read, with defaults
    froms: tuple[str, ...]  # each FROM's image as written, without --platform or AS


def read(path: Path) -> Compose:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ComposeError(path, ["is missing"]) from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise ComposeError(path, [f"cannot be read: {exc}"]) from exc
    return parse_text(text, path)


def parse_text(text: str, path: Path) -> Compose:
    try:
        data = yaml_subset.load(text)
    except yaml_subset.YamlError as exc:
        raise ComposeError(path, [str(exc)]) from exc
    problems: list[str] = []
    services: dict[str, Service] = {}
    # This reader and `docker compose config` must see the same services: neither key is read here, so a file
    # that uses one is refused rather than read as less than docker sees.
    if isinstance(data, dict) and "include" in data:
        problems.append("include is not read by the factory: put every service in this one file")
    found = data.get("services") if isinstance(data, dict) else None
    if not isinstance(found, dict) or not found:
        problems.append("has no services mapping")
        found = {}
    for name, entry in found.items():
        if not isinstance(entry, dict):
            problems.append(f"service {name}: must be a mapping")
            continue
        if "extends" in entry:
            problems.append(f"service {name}: extends is not read by the factory: write the service out whole")
        services[name] = _service(name, entry, problems)
    networks = _networks(data.get("networks") if isinstance(data, dict) else None, problems)
    if problems:
        raise ComposeError(path, problems)
    return Compose(path, services, networks)


def _service(name: str, entry: dict[str, Any], problems: list[str]) -> Service:
    where = f"service {name}"
    image = entry.get("image")
    if image is not None and not isinstance(image, str):
        problems.append(f"{where}: image must be a string")
        image = None
    mode = entry.get("network_mode")
    if mode is not None and not isinstance(mode, str):
        problems.append(f"{where}: network_mode must be a string")
        mode = None
    nets = entry.get("networks") or []
    if not isinstance(nets, list | dict):
        problems.append(f"{where}: networks must be a list or a mapping")
        nets = []
    health = entry.get("healthcheck")
    healthcheck = isinstance(health, dict) and health.get("disable") is not True and health.get("test") != ["NONE"]
    return Service(
        name,
        _build(where, entry.get("build"), problems) if "build" in entry else None,
        image,
        _binds(where, entry.get("volumes"), problems),
        _ports(where, entry.get("ports"), problems),
        tuple(str(n) for n in nets),
        mode,
        healthcheck,
    )


def _text(where: str, value: Any, problems: list[str]) -> str:
    if isinstance(value, str) and value:
        return value
    problems.append(f"{where} must be a non-empty string")
    return ""


def _build(where: str, value: Any, problems: list[str]) -> Build:
    if isinstance(value, str):
        return Build(_text(f"{where}: build", value, problems), "Dockerfile", (), None)
    if not isinstance(value, dict):
        problems.append(f"{where}: build must be a path or a mapping")
        return Build("", "", (), None)
    # Compose's own defaults: the project folder and the Dockerfile in it.
    context = _text(f"{where}: build.context", value.get("context", "."), problems)
    dockerfile = _text(f"{where}: build.dockerfile", value.get("dockerfile", "Dockerfile"), problems)
    target = value.get("target")
    if target is not None:
        target = _text(f"{where}: build.target", target, problems)
    return Build(context, dockerfile, _args(where, value.get("args"), problems), target)


def _args(where: str, value: Any, problems: list[str]) -> tuple[tuple[str, str | None], ...]:
    pairs: list[tuple[str, Any]] = []
    if value is None:
        return ()
    if isinstance(value, dict):
        pairs = list(value.items())
    elif isinstance(value, list) and all(isinstance(v, str) for v in value):
        for item in value:
            key, eq, rest = item.partition("=")
            pairs.append((key, rest if eq else None))
    else:
        problems.append(f"{where}: build.args must be a mapping or a list of KEY=value")
        return ()
    found: list[tuple[str, str | None]] = []
    for key, raw in pairs:
        if raw is None or isinstance(raw, str):
            found.append((key, raw))
        elif isinstance(raw, int | float) and not isinstance(raw, bool):
            found.append((key, str(raw)))
        else:
            problems.append(f"{where}: build arg {key} must be a string or a number")
    return tuple(found)


def _relative(source: str) -> bool:
    return source in (".", "..") or source.startswith(("./", "../"))


def _binds(where: str, value: Any, problems: list[str]) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        problems.append(f"{where}: volumes must be a list")
        return ()
    found: list[str] = []
    for item in value:
        if isinstance(item, str):
            source = item.split(":", 1)[0]
        elif isinstance(item, dict) and item.get("type", "volume") == "bind":
            source = str(item.get("source", ""))
        else:
            continue
        if _relative(source):
            found.append(os.path.normpath(source))
    return tuple(dict.fromkeys(found))


def _split_port(text: str) -> list[str]:
    """A short port entry's fields: colons split it, except inside ``${...}`` and an IPv6 address's brackets."""
    fields, depth, start = [], 0, 0
    for index, char in enumerate(text):
        if char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
        elif char == ":" and depth == 0:
            fields.append(text[start:index])
            start = index + 1
    return [*fields, text[start:]]


def _ports(where: str, value: Any, problems: list[str]) -> tuple[Port, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        problems.append(f"{where}: ports must be a list")
        return ()
    found: list[Port] = []
    for item in value:
        if isinstance(item, dict):
            published = item.get("published")
            found.append(Port(str(item.get("host_ip", "")), "" if published is None else str(published),
                              str(item.get("target", "")), str(item)))  # fmt: skip
            continue
        if not isinstance(item, str | int) or isinstance(item, bool):
            problems.append(f"{where}: a ports entry must be a string, a number or a mapping")
            continue
        text = str(item)
        fields = _split_port(text)
        if len(fields) > 3:
            problems.append(f"{where}: port {text!r} cannot be read; write host_ip:published:target")
            continue
        fields = [""] * (3 - len(fields)) + fields
        if len(fields[0]) > 1 and fields[0].startswith("[") and fields[0].endswith("]"):
            fields[0] = fields[0][1:-1]
        found.append(Port(fields[0], fields[1], fields[2], text))
    return tuple(found)


def _networks(value: Any, problems: list[str]) -> dict[str, Network]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        problems.append("networks must be a mapping")
        return {}
    found: dict[str, Network] = {}
    for key, entry in value.items():
        entry = entry or {}
        if not isinstance(entry, dict):
            problems.append(f"network {key}: must be a mapping")
            continue
        external, name = entry.get("external", False), entry.get("name", "")
        if not isinstance(external, bool):
            problems.append(f"network {key}: external must be true or false (name the network with name:)")
            continue
        if not isinstance(name, str):
            problems.append(f"network {key}: name must be a string")
            continue
        found[key] = Network(external, name)
    return found


def dockerfile(path: Path) -> Dockerfile:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ComposeError(path, ["is missing"]) from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise ComposeError(path, [f"cannot be read: {exc}"]) from exc
    problems: list[str] = []
    args: dict[str, str | None] = {}
    froms: list[str] = []
    for number, line in _instructions(text):
        word, _, rest = line.partition(" ")
        word = word.upper()
        if word == "ARG" and not froms:
            _arg(rest, number, args, problems)
        elif word == "FROM":
            image = [w for w in rest.split() if not w.startswith("--")]
            if not image:
                problems.append(f"line {number}: FROM names no image")
                continue
            froms.append(image[0])
    if not froms:
        problems.append("has no FROM line")
    if problems:
        raise ComposeError(path, problems)
    return Dockerfile(args, tuple(froms))


def _arg(rest: str, number: int, args: dict[str, str | None], problems: list[str]) -> None:
    """A global ARG line's names into ``args``, each with its default or None."""
    try:
        words = shlex.split(rest)
    # telemetry: surfaced — an ARG line that cannot be read is one of the Dockerfile's `problems`
    except ValueError as exc:
        problems.append(f"line {number}: ARG cannot be read: {exc}")
        return
    for item in words:
        key, eq, default = item.partition("=")
        args[key] = default if eq else None


def _instructions(text: str) -> list[tuple[int, str]]:
    """Each instruction with its first line's number: continued lines joined, comment lines dropped."""
    found: list[tuple[int, str]] = []
    parts: list[str] = []
    start = 0
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if line.startswith("#") or (not line and parts):
            continue
        if not parts:
            start = number
        if line.endswith("\\"):
            parts.append(line[:-1].strip())
            continue
        parts.append(line)
        joined = " ".join(p for p in parts if p)
        if joined:
            found.append((start, joined))
        parts = []
    if parts:
        found.append((start, " ".join(p for p in parts if p)))
    return found
