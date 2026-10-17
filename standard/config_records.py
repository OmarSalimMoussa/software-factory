"""What factory.toml holds once it is read: one frozen record per table, and the whole file as one Config. The
config module reads the file into them and checks every value there.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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
class Reports:
    port: int
    path: str
    header: str
    token_key: str


@dataclass(frozen=True, slots=True)
class Config:
    product: Product
    build: Build
    services: Services
    env: Env
    server: Server
    workspaces: tuple[Workspace, ...]
    deploy: Deploy
    edge: Edge
    reports: Reports | None  # None: the product has no report export
    raw: dict[str, Any]  # the parsed file, for comparing two copies

    def workspace(self, name: str) -> Workspace:
        for ws in self.workspaces:
            if ws.name == name:
                return ws
        raise KeyError(name)

    def image(self, image: str, tag: str) -> str:
        return f"{self.product.registry}/{self.product.name}-{image}:{tag}"

    def project(self, workspace: str) -> str:
        """The compose project of a workspace (``prod``) or a slot (``slot2``)."""
        return f"{self.product.name}-{workspace}"

    def hooks(self) -> tuple[str, ...]:
        """Every script factory.toml names: each is protected like factory.toml itself."""
        return tuple(dict.fromkeys(hook for ws in self.workspaces for hook in ws.after_deploy))
