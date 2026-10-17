"""factory.toml: the one file a product sets for the standard build and deploy.

It sits at the top of the product's repo and only a person changes it. Every key is required unless noted, an
unknown key is an error, and every problem is reported at once. The same file drives the build plan, the server's
set-up and prod's deploys, the stack slots and the warden's view of prod.
"""

from __future__ import annotations

import re
import string
import tomllib
from pathlib import Path
from typing import Any

from standard.config_records import (
    Build,
    Config,
    Deploy,
    Edge,
    Env,
    ExtraImage,
    Product,
    Reports,
    Route,
    Server,
    Services,
    Workspace,
)
from standard.toml_fields import Fields

FILE = "factory.toml"
REQUIRED = ("product", "build", "services", "env", "server", "workspaces", "deploy", "edge")
WORKSPACES = ("prod",)
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
_HEADER = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")
_URL_PATH = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*$")
_BLOCKED = re.compile(r"^/[A-Za-z0-9._~!$&'()+,;=:@%/-]*\*?$")


class ConfigError(Exception):
    """factory.toml is missing, unreadable or breaks the schema; ``problems`` lists every one."""

    def __init__(self, path: Path, problems: list[str]) -> None:
        self.path = path
        self.problems = problems
        super().__init__(f"{path}: " + "; ".join(problems))


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


class _Reader(Fields):
    """Walks the parsed file, collecting every problem rather than stopping at the first."""

    def config(self, data: dict[str, Any]) -> Config | None:
        self.known(data, "factory.toml", (*REQUIRED, "reports"))
        product = self.product(data)
        build = self.build(data)
        services = self.services(data)
        env = self.env(data)
        server = self.server(data)
        workspaces = self.workspaces(data)
        deploy = self.deploy(data)
        edge = self.edge(data)
        reports = self.reports(data)
        if self.problems:
            return None
        return Config(product, build, services, env, server, workspaces, deploy, edge, reports, data)

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
        # telemetry: surfaced — a value that is no format string is a problem through `fail`; the load raises them
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
        t = self.table(data, "workspaces", "", keys=WORKSPACES)
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

    def reports(self, data: dict[str, Any]) -> Reports | None:
        if "reports" not in data:
            return None
        t = self.table(data, "reports", "", ("port", "path", "header", "token_key"))
        return Reports(
            self.number(t, "port", "[reports]", 1, 65535),
            self.text(t, "path", "[reports]", _URL_PATH, "a path starting with /"),
            self.text(t, "header", "[reports]", _HEADER, "an HTTP header name"),
            self.text(t, "token_key", "[reports]", _ENV_KEY, "an UPPER_CASE environment key"),
        )
