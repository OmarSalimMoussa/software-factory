"""factory.toml: the one file a product sets for the standard build and deploy.

It sits at the top of the product's repo and only a person changes it. Every key is required unless noted, an
unknown key is an error, and every problem is reported at once. The same file drives CI's build plan and smoke test,
the server's set-up and deploys, the laptop's stack slots and the warden's view of staging and prod.
"""

from __future__ import annotations

import re
import string
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FILE = "factory.toml"
# One line, owner/name@<40-character sha>: the factory commit the product's CI plans and checks with.
FACTORY_REF = ".github/factory-ref"
FACTORY_REF_LINE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@([0-9a-f]{40})")
WORKSPACES = ("staging", "prod")
RELEASE_KINDS = ("chrome-web-store",)
# What an [env.set] value may name; each workspace and slot fills them in.
PLACEHOLDERS = ("workspace", "origin", "port", "commit", "dir", "env_dir", "docker_socket")

_NAME = re.compile(r"^[a-z][a-z0-9-]{0,19}$")
_IMAGE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
_REGISTRY = re.compile(r"^[a-z0-9.-]+(?::\d+)?(?:/[a-z0-9._-]+)+$")
_HOST = re.compile(r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")
_HOST_KEY = re.compile(r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)) [A-Za-z0-9+/=]+$")
_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")
_ARG = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_URL_PATH = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*$")
_BLOCKED = re.compile(r"^/[A-Za-z0-9._~!$&'()+,;=:@%/-]*\*?$")


class ConfigError(Exception):
    """factory.toml is missing, unreadable or breaks the schema; ``problems`` lists every one."""

    def __init__(self, path: Path, problems: list[str]) -> None:
        self.path = path
        self.problems = problems
        super().__init__(f"{path}: " + "; ".join(problems))


@dataclass(frozen=True, slots=True)
class Product:
    name: str
    registry: str


@dataclass(frozen=True, slots=True)
class ExtraImage:
    name: str
    dockerfile: str
    context: str


@dataclass(frozen=True, slots=True)
class Build:
    commit_arg: str
    tag_arg: str
    extra_images: tuple[ExtraImage, ...]


@dataclass(frozen=True, slots=True)
class Endpoint:
    service: str
    port: int


@dataclass(frozen=True, slots=True)
class Expect:
    service: str
    port: int
    path: str
    status: int


@dataclass(frozen=True, slots=True)
class Smoke:
    timeout_seconds: int
    health: Endpoint
    expect: tuple[Expect, ...]
    check: str  # "" when the product has no smoke check of its own


@dataclass(frozen=True, slots=True)
class Services:
    owner_email: str
    errors: tuple[str, ...]
    errors_public_path: str  # "" when browsers do not report through the product's own origin


@dataclass(frozen=True, slots=True)
class Env:
    file: str
    template: str
    set: tuple[tuple[str, str], ...]  # (key, format string), in file order


@dataclass(frozen=True, slots=True)
class Server:
    host: str
    host_key: str
    admin: str
    tailnet: str
    acme_email: str


@dataclass(frozen=True, slots=True)
class Workspace:
    name: str
    domain: str
    port: int
    after_deploy: tuple[str, ...]

    @property
    def origin(self) -> str:
        return f"https://{self.domain}"


@dataclass(frozen=True, slots=True)
class Deploy:
    health: str
    answer_seconds: int
    drain_minutes: int
    files: tuple[str, ...]
    keep_images: int


@dataclass(frozen=True, slots=True)
class Route:
    host: str
    redirect: str


@dataclass(frozen=True, slots=True)
class Edge:
    blocked_paths: tuple[str, ...]
    routes: tuple[Route, ...]


@dataclass(frozen=True, slots=True)
class Release:
    kind: str
    when_changed: tuple[str, ...]
    package: str
    live: bool


@dataclass(frozen=True, slots=True)
class Config:
    product: Product
    build: Build
    smoke: Smoke
    services: Services
    env: Env
    server: Server
    workspaces: tuple[Workspace, ...]  # staging, then prod
    deploy: Deploy
    edge: Edge
    releases: tuple[Release, ...]
    raw: dict[str, Any]  # the parsed file, for comparing two copies

    def workspace(self, name: str) -> Workspace:
        for ws in self.workspaces:
            if ws.name == name:
                return ws
        raise KeyError(name)

    def image(self, image: str, tag: str) -> str:
        return f"{self.product.registry}/{self.product.name}-{image}:{tag}"

    def project(self, workspace: str) -> str:
        """The compose project of a workspace (``prod``) or a laptop slot (``slot2``)."""
        return f"{self.product.name}-{workspace}"

    def hooks(self) -> tuple[str, ...]:
        """Every script factory.toml names: each is protected like factory.toml itself."""
        found = [self.smoke.check] if self.smoke.check else []
        for ws in self.workspaces:
            found += ws.after_deploy
        found += [r.package for r in self.releases]
        return tuple(dict.fromkeys(found))


def render(template: str, values: dict[str, str]) -> str:
    """One [env.set] value with its placeholders filled; every placeholder must be in ``values``."""
    return template.format(**values)


def load(repo: Path) -> Config:
    return parse(repo / FILE)


def parse(path: Path) -> Config:
    try:
        text = path.read_text()
    except FileNotFoundError as exc:
        raise ConfigError(path, [f"{FILE} is missing: every product sets one at the top of its repo"]) from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(path, [f"cannot be read: {exc}"]) from exc
    return parse_text(text, path)


def parse_text(text: str, path: Path = Path(FILE)) -> Config:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(path, [f"is not valid TOML: {exc}"]) from exc
    reader = _Reader()
    config = reader.config(data)
    if reader.problems or config is None:
        raise ConfigError(path, reader.problems)
    return config


class _Reader:
    """Walks the parsed file, collecting every problem rather than stopping at the first."""

    def __init__(self) -> None:
        self.problems: list[str] = []

    def fail(self, where: str, what: str) -> None:
        self.problems.append(f"{where}: {what}")

    def table(
        self, data: dict[str, Any], key: str, where: str, keys: tuple[str, ...], optional: tuple[str, ...] = ()
    ) -> dict[str, Any]:
        here = f"{where}.{key}" if where else f"[{key}]"
        value = data.get(key)
        if not isinstance(value, dict):
            self.fail(here, "missing" if value is None else "must be a table")
            return {}
        self.known(value, here, keys + optional)
        for name in keys:
            if name not in value:
                self.fail(f"{here}.{name}", "missing")
        return value

    def known(self, value: dict[str, Any], where: str, keys: tuple[str, ...]) -> None:
        for name in value:
            if name not in keys:
                self.fail(f"{where}.{name}", f"unknown key (allowed: {', '.join(keys)})")

    def text(
        self, t: dict[str, Any], key: str, where: str, pattern: re.Pattern[str] | None = None, what: str = ""
    ) -> str:
        value = t.get(key)
        if key not in t:
            return ""
        if not isinstance(value, str) or not value:
            self.fail(f"{where}.{key}", "must be a non-empty string")
            return ""
        if pattern is not None and not pattern.fullmatch(value):
            self.fail(f"{where}.{key}", f"{value!r} is not {what or 'well formed'}")
        return value

    def number(self, t: dict[str, Any], key: str, where: str, low: int, high: int) -> int:
        value = t.get(key)
        if key not in t:
            return 0
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            self.fail(f"{where}.{key}", f"must be a whole number from {low} to {high}")
            return 0
        return value

    def flag(self, t: dict[str, Any], key: str, where: str) -> bool:
        value = t.get(key)
        if key in t and not isinstance(value, bool):
            self.fail(f"{where}.{key}", "must be true or false")
        return value is True

    def texts(
        self, t: dict[str, Any], key: str, where: str, check: Any = None, empty_ok: bool = True
    ) -> tuple[str, ...]:
        value = t.get(key)
        if key not in t:
            return ()
        if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
            self.fail(f"{where}.{key}", "must be a list of non-empty strings")
            return ()
        if not value and not empty_ok:
            self.fail(f"{where}.{key}", "must name at least one")
        if len(set(value)) != len(value):
            self.fail(f"{where}.{key}", "names something twice")
        if check is not None:
            for item in value:
                check(f"{where}.{key}", item)
        return tuple(value)

    def rel_path(self, where: str, value: str) -> None:
        parts = Path(value).parts
        if value.startswith(("/", "~")) or ".." in parts:
            self.fail(where, f"{value!r} must be a path inside the repo, relative to its top")

    def path_key(self, t: dict[str, Any], key: str, where: str, optional: bool = False) -> str:
        if optional and t.get(key, "") == "":
            if key in t and not isinstance(t[key], str):
                self.fail(f"{where}.{key}", "must be a string")
            return ""
        value = self.text(t, key, where)
        if value:
            self.rel_path(f"{where}.{key}", value)
        return value

    def items(self, t: dict[str, Any], key: str, where: str) -> list[tuple[str, dict[str, Any]]]:
        value = t.get(key)
        if key not in t:
            return []
        if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
            self.fail(f"{where}.{key}", "must be a list of tables")
            return []
        return [(f"{where}.{key}[{n}]", item) for n, item in enumerate(value)]

    def config(self, data: dict[str, Any]) -> Config | None:
        self.known(
            data,
            "factory.toml",
            ("product", "build", "smoke", "services", "env", "server", "workspaces", "deploy", "edge", "release"),
        )
        product = self.product(data)
        build = self.build(data)
        smoke = self.smoke(data)
        services = self.services(data)
        env = self.env(data)
        server = self.server(data)
        workspaces = self.workspaces(data)
        deploy = self.deploy(data)
        edge = self.edge(data)
        releases = self.releases(data)
        if self.problems:
            return None
        return Config(product, build, smoke, services, env, server, workspaces, deploy, edge, releases, data)

    def product(self, data: dict[str, Any]) -> Product:
        t = self.table(data, "product", "", ("name", "registry"))
        return Product(
            self.text(t, "name", "[product]", _NAME, "a short lowercase name (letters, digits, -)"),
            self.text(t, "registry", "[product]", _REGISTRY, "a lowercase registry path"),
        )

    def build(self, data: dict[str, Any]) -> Build:
        t = self.table(data, "build", "", ("commit_arg", "tag_arg", "extra_images"))
        extras = []
        for where, item in self.items(t, "extra_images", "[build]"):
            self.known(item, where, ("name", "dockerfile", "context"))
            name = self.text(item, "name", where, _IMAGE, "an image name (lowercase letters, digits, -)")
            if name == "deploy":
                self.fail(f"{where}.name", "'deploy' is the factory's own image")
            dockerfile, context = self.path_key(item, "dockerfile", where), self.path_key(item, "context", where)
            for key in ("name", "dockerfile", "context"):
                if key not in item:
                    self.fail(f"{where}.{key}", "missing")
            extras.append(ExtraImage(name, dockerfile, context))
        names = [e.name for e in extras]
        if len(set(names)) != len(names):
            self.fail("[build].extra_images", "names an image twice")
        return Build(
            self.text(t, "commit_arg", "[build]", _ARG, "a build argument name"),
            self.text(t, "tag_arg", "[build]", _ARG, "a build argument name"),
            tuple(extras),
        )

    def endpoint(self, item: Any, where: str) -> Endpoint:
        if not isinstance(item, dict):
            self.fail(where, "missing" if item is None else "must be a table {service, port}")
            return Endpoint("", 0)
        self.known(item, where, ("service", "port"))
        return Endpoint(
            self.text(item, "service", where) or self._missing(item, "service", where),
            self.number(item, "port", where, 1, 65535) or self._missing_int(item, "port", where),
        )

    def _missing(self, t: dict[str, Any], key: str, where: str) -> str:
        if key not in t:
            self.fail(f"{where}.{key}", "missing")
        return ""

    def _missing_int(self, t: dict[str, Any], key: str, where: str) -> int:
        if key not in t:
            self.fail(f"{where}.{key}", "missing")
        return 0

    def smoke(self, data: dict[str, Any]) -> Smoke:
        t = self.table(data, "smoke", "", ("timeout_seconds", "health", "expect"), optional=("check",))
        expects = []
        for where, item in self.items(t, "expect", "[smoke]"):
            self.known(item, where, ("service", "port", "path", "status"))
            expects.append(
                Expect(
                    self.text(item, "service", where) or self._missing(item, "service", where),
                    self.number(item, "port", where, 1, 65535) or self._missing_int(item, "port", where),
                    self.text(item, "path", where, _URL_PATH, "a path starting with /")
                    or self._missing(item, "path", where),
                    self.number(item, "status", where, 100, 599) or self._missing_int(item, "status", where),
                )
            )
        health = self.endpoint(t["health"], "[smoke].health") if "health" in t else Endpoint("", 0)
        return Smoke(
            self.number(t, "timeout_seconds", "[smoke]", 10, 3600),
            health,
            tuple(expects),
            self.path_key(t, "check", "[smoke]", optional=True),
        )

    def services(self, data: dict[str, Any]) -> Services:
        t = self.table(data, "services", "", ("owner_email", "errors"), optional=("errors_public_path",))

        def project(where: str, name: str) -> None:
            if not _IMAGE.fullmatch(name):
                self.fail(where, f"{name!r} is not a project name (lowercase letters, digits, -)")

        public = t.get("errors_public_path", "")
        if public and (not isinstance(public, str) or not _URL_PATH.fullmatch(public) or public.endswith("/")):
            self.fail("[services].errors_public_path", "must be a path such as /telemetry (no trailing /)")
            public = ""
        return Services(
            self.text(t, "owner_email", "[services]", _EMAIL, "an email address"),
            self.texts(t, "errors", "[services]", project),
            public if isinstance(public, str) else "",
        )

    def env(self, data: dict[str, Any]) -> Env:
        t = self.table(data, "env", "", ("file", "template", "set"))
        pairs: list[tuple[str, str]] = []
        values = t.get("set")
        if "set" in t and not isinstance(values, dict):
            self.fail("[env].set", 'must be a table of KEY = "format string"')
        elif isinstance(values, dict):
            for key, value in values.items():
                where = f"[env.set].{key}"
                if not _ENV_KEY.fullmatch(key):
                    self.fail(where, "must be an UPPER_CASE environment key")
                if not isinstance(value, str):
                    self.fail(where, "must be a string")
                    continue
                self.placeholders(where, value)
                pairs.append((key, value))
        file_name, template = self.path_key(t, "file", "[env]"), self.path_key(t, "template", "[env]")
        if file_name and template and file_name == template:
            self.fail("[env]", "file and template must differ")
        return Env(file_name, template, tuple(pairs))

    def placeholders(self, where: str, value: str) -> None:
        try:
            fields = [f for _, f, _, _ in string.Formatter().parse(value) if f is not None]
        except ValueError as exc:
            self.fail(where, f"is not a format string ({exc}); write a literal brace as {{{{ or }}}}")
            return
        for field in fields:
            if field not in PLACEHOLDERS:
                self.fail(where, f"{{{field}}} is not one of {', '.join('{' + p + '}' for p in PLACEHOLDERS)}")

    def server(self, data: dict[str, Any]) -> Server:
        t = self.table(data, "server", "", ("host", "host_key", "admin", "tailnet", "acme_email"))
        return Server(
            self.text(t, "host", "[server]", _HOST, "a host name"),
            self.text(t, "host_key", "[server]", _HOST_KEY, "an SSH public key line (type and key)"),
            self.text(t, "admin", "[server]", _USER, "a user name"),
            self.text(t, "tailnet", "[server]", _HOST, "a tailnet host name"),
            self.text(t, "acme_email", "[server]", _EMAIL, "an email address"),
        )

    def workspaces(self, data: dict[str, Any]) -> tuple[Workspace, ...]:
        t = self.table(data, "workspaces", "", WORKSPACES)
        found = []
        for name in WORKSPACES:
            item = t.get(name)
            where = f"[workspaces.{name}]"
            if not isinstance(item, dict):
                if name in t:
                    self.fail(where, "must be a table")
                continue
            self.known(item, where, ("domain", "port", "after_deploy"))
            for key in ("domain", "port", "after_deploy"):
                if key not in item:
                    self.fail(f"{where}.{key}", "missing")
            found.append(
                Workspace(
                    name,
                    self.text(item, "domain", where, _HOST, "a host name"),
                    self.number(item, "port", where, 1024, 65535),
                    self.texts(item, "after_deploy", where, self.rel_path),
                )
            )
        if len(found) == 2:
            if found[0].domain and found[0].domain == found[1].domain:
                self.fail("[workspaces]", "staging and prod need different domains")
            if found[0].port and found[0].port == found[1].port:
                self.fail("[workspaces]", "staging and prod need different ports")
        return tuple(found)

    def deploy(self, data: dict[str, Any]) -> Deploy:
        t = self.table(data, "deploy", "", ("health", "answer_seconds", "drain_minutes", "files", "keep_images"))
        return Deploy(
            self.text(t, "health", "[deploy]", _URL_PATH, "a path starting with /"),
            self.number(t, "answer_seconds", "[deploy]", 10, 3600),
            self.number(t, "drain_minutes", "[deploy]", 0, 240),
            self.texts(t, "files", "[deploy]", self.rel_path, empty_ok=False),
            self.number(t, "keep_images", "[deploy]", 1, 50),
        )

    def edge(self, data: dict[str, Any]) -> Edge:
        t = self.table(data, "edge", "", ("blocked_paths", "routes"))

        def blocked(where: str, value: str) -> None:
            if not _BLOCKED.fullmatch(value):
                self.fail(where, f"{value!r} must be a path starting with /, optionally ending in *")

        routes = []
        for where, item in self.items(t, "routes", "[edge]"):
            self.known(item, where, ("host", "redirect"))
            host = self.text(item, "host", where, _HOST, "a host name") or self._missing(item, "host", where)
            to = self.text(item, "redirect", where, _HOST, "a host name") or self._missing(item, "redirect", where)
            routes.append(Route(host, to))
        return Edge(self.texts(t, "blocked_paths", "[edge]", blocked), tuple(routes))

    def releases(self, data: dict[str, Any]) -> tuple[Release, ...]:
        found = []
        for where, item in self.items(data, "release", "[[release]]"):
            self.known(item, where, ("kind", "when_changed", "package", "live"))
            for key in ("kind", "when_changed", "package", "live"):
                if key not in item:
                    self.fail(f"{where}.{key}", "missing")
            kind = self.text(item, "kind", where)
            if kind and kind not in RELEASE_KINDS:
                self.fail(f"{where}.kind", f"{kind!r} is not one of {', '.join(RELEASE_KINDS)}")
            found.append(
                Release(
                    kind,
                    self.texts(item, "when_changed", where, self.rel_path, empty_ok=False),
                    self.path_key(item, "package", where),
                    self.flag(item, "live", where),
                )
            )
        return tuple(found)
