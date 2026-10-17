from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from standard import yaml_subset
from standard.config import parse_text
from standard.config_records import Config
from standard.services import stack

FACTORY_TOML = """
[product]
name = "shop"
registry = "ghcr.io/acme"

[build]
commit_arg = "SHOP_COMMIT"
tag_arg = "SHOP_TAG"
extra_images = []

[services]
owner_email = "owner@acme.dev"
errors = ["web"]

[env]
file = "shop.env"
template = "shop.env.template"

[env.set]
SHOP_WORKSPACE = "{workspace}"

[server]
host = "prod.acme.dev"
host_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHO"
admin = "deploy"
tailnet = "shop-box.tail0.ts.net"
acme_email = "ops@acme.dev"

[workspaces.prod]
domain = "prod.acme.dev"
port = 8100
after_deploy = []

[deploy]
health = "/healthz"
answer_seconds = 180
drain_minutes = 15
files = ["compose.yml"]
keep_images = 3

[edge]
blocked_paths = []
routes = []
"""

CONFIG: Config = parse_text(FACTORY_TOML)
CONTAINERS = {
    "factory-postgres", "factory-pgweb", "factory-langfuse", "factory-langfuse-worker", "factory-langfuse-clickhouse",
    "factory-langfuse-minio", "factory-langfuse-redis", "factory-langfuse-postgres", "factory-glitchtip",
    "factory-glitchtip-worker", "factory-glitchtip-postgres", "factory-glitchtip-valkey", "factory-edge",
}  # fmt: skip


def test_the_compose_file_names_the_containers_reads_every_value_and_publishes_on_loopback_only() -> None:
    text = stack.COMPOSE.read_text()
    services = yaml_subset.load(text)["services"]

    assert {body["container_name"] for body in services.values()} >= CONTAINERS
    published = [port for body in services.values() for port in body.get("ports", [])]
    assert published and all(port.startswith("127.0.0.1:") for port in published)
    assert services["edge"]["network_mode"] == "host"
    read_by_the_provisioner_alone = {"GLITCHTIP_OWNER_PASSWORD"}
    made = set(stack.generate_env(CONFIG, existing={})) - read_by_the_provisioner_alone
    assert set(re.findall(r"\$\{([A-Z0-9_]+):\?\}", text)) == made


def test_generate_env_keeps_existing_secrets_fills_missing_ones_and_follows_the_host() -> None:
    existing = {"POSTGRES_PASSWORD": "kept", "NEXTAUTH_URL": "http://old", "GONE": "x"}
    base = "https://shop-box.tail0.ts.net"

    values = stack.generate_env(CONFIG, existing=existing)

    assert values["POSTGRES_PASSWORD"] == "kept"
    assert re.fullmatch(r"pk-lf-[0-9a-f]{32}", values["LANGFUSE_INIT_PROJECT_PUBLIC_KEY"])
    assert re.fullmatch(r"sk-lf-[0-9a-f]{32}", values["LANGFUSE_INIT_PROJECT_SECRET_KEY"])
    assert len(values["LANGFUSE_ENCRYPTION_KEY"]) == 64 and values["GLITCHTIP_OWNER_PASSWORD"]
    assert (values["NEXTAUTH_URL"], values["GLITCHTIP_DOMAIN"]) == (f"{base}:3100", f"{base}:8200")
    assert values["LANGFUSE_S3_MEDIA_UPLOAD_ENDPOINT"] == f"{base}:9090"
    assert (values["LANGFUSE_INIT_ORG_ID"], values["LANGFUSE_INIT_PROJECT_ID"]) == ("shop", "shop")
    assert values["LANGFUSE_INIT_USER_EMAIL"] == "owner@acme.dev" and values["ACME_EMAIL"] == "ops@acme.dev"
    assert "GONE" not in values
    assert stack.generate_env(CONFIG, existing=values) == values


def test_up_names_the_service_that_did_not_turn_healthy(tmp_path: Path) -> None:
    rows = [{"Service": name, "State": "running", "Health": "healthy", "ExitCode": 0} for name in stack.services()]
    rows = [r | {"State": "exited"} if r["Service"] in stack.ONE_SHOT else r for r in rows]
    rows = [r | {"Health": "unhealthy"} if r["Service"] == "langfuse" else r for r in rows]
    calls: list[list[str]] = []

    def run(argv: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if "up" in argv:
            return subprocess.CompletedProcess(argv, 1, "", "dependency failed to start")
        return subprocess.CompletedProcess(argv, 0, "\n".join(json.dumps(r) for r in rows), "")

    with pytest.raises(stack.StackError, match=r"langfuse \(running, unhealthy\)"):
        stack.up(run, tmp_path)

    assert calls[0] == [
        "docker", "compose", "-p", "factory-services", "--env-file", f"{tmp_path}/services.env",
        "-f", f"{tmp_path}/compose.yml", "up", "-d", "--wait", "--wait-timeout", "1200",
    ]  # fmt: skip


def test_every_image_is_pinned() -> None:
    images = re.findall(r"^\s+image: (\S+)$", stack.COMPOSE.read_text(), re.M)
    # A digest, or a version to its last part: postgres numbers its releases in two parts (17.10), the rest in three.
    exact = re.compile(r"(@sha256:|/postgres:\d+\.\d+$|:\d+\.\d+\.\d+)")
    floating = [i for i in images if not exact.search(i)]
    assert floating == [], f"pin each to an exact version: {floating}"
