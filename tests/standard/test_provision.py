from __future__ import annotations

from pathlib import Path

from deploy_kit import FakeDocker, config

from standard.services import provision

SERVICES = {
    "LANGFUSE_INIT_PROJECT_PUBLIC_KEY": "pk-lf-1",
    "LANGFUSE_INIT_PROJECT_SECRET_KEY": "sk-lf-1",
    "GLITCHTIP_OWNER_PASSWORD": "owner-secret",
}
SHARED = {"factory-postgres", "factory-langfuse", "factory-glitchtip"}


def provision_slot(docker: FakeDocker, state: Path) -> dict[str, str]:
    return provision.workspace(
        config(), SERVICES, docker, workspace="slot2", origin="http://127.0.0.1:8402", commit="dev-slot2", state=state
    )


def test_a_second_run_creates_nothing_and_keeps_the_password(tmp_path: Path) -> None:
    docker = FakeDocker()
    first = provision_slot(docker, tmp_path)
    first_calls = docker.argvs()
    docker.calls.clear()

    second = provision_slot(docker, tmp_path)

    assert second == first
    assert ["docker", "network", "create", "notes-slot2-services"] in first_calls
    assert not [argv for argv in docker.argvs() if argv[1:3] in (["network", "create"], ["network", "connect"])]
    assert docker.networks == {"notes-slot2-services": SHARED, "notes-slot2-telemetry": set(provision.TELEMETRY)}
    assert (tmp_path / "notes_slot2.password").stat().st_mode & 0o777 == 0o600


def test_identifiers_and_secrets_are_quoted_and_never_in_a_command_line(tmp_path: Path) -> None:
    docker = FakeDocker()
    values = provision_slot(docker, tmp_path)
    password = (tmp_path / "notes_slot2.password").read_text().strip()

    sql = next(stdin for argv, stdin in docker.calls if "psql" in argv)
    assert sql is not None
    assert f"ALTER ROLE \"notes_slot2\" WITH LOGIN PASSWORD '{password}';" in sql
    assert "format('CREATE DATABASE %I OWNER %I', 'notes_slot2', 'notes_slot2')" in sql
    assert 'REVOKE ALL ON DATABASE "notes_slot2" FROM PUBLIC;' in sql
    provision.bootstrap(config(), SERVICES, docker)
    every_argv = " ".join(" ".join(argv) for argv in docker.argvs())
    assert password not in every_argv and "owner-secret" not in every_argv
    assert values["DATABASE_URL"] == f"postgresql://notes_slot2:{password}@factory-postgres:5432/notes_slot2"


def test_the_values_name_the_shared_services_inside_docker_and_the_origin_for_browsers(tmp_path: Path) -> None:
    values = provision_slot(FakeDocker(), tmp_path)

    assert {k: v for k, v in values.items() if k != "DATABASE_URL"} == {
        "FACTORY_SERVICES_NETWORK": "notes-slot2-services",
        "FACTORY_TELEMETRY_NETWORK": "notes-slot2-telemetry",
        "LANGFUSE_HOST": "http://factory-langfuse:3000",
        "LANGFUSE_PUBLIC_KEY": "pk-lf-1",
        "LANGFUSE_SECRET_KEY": "sk-lf-1",
        "LANGFUSE_TRACING_ENVIRONMENT": "slot2",
        "SENTRY_ENVIRONMENT": "slot2",
        "SENTRY_RELEASE": "dev-slot2",
        "SENTRY_INGEST_URL": "http://factory-glitchtip:8000",
        "SENTRY_DSN_APP": "http://key2@factory-glitchtip:8000/2",
        "SENTRY_PUBLIC_DSN_APP": "http://key2@127.0.0.1:8402/telemetry/2",
        "SENTRY_DSN_WEB_UI": "http://key3@factory-glitchtip:8000/3",
        "SENTRY_PUBLIC_DSN_WEB_UI": "http://key3@127.0.0.1:8402/telemetry/3",
    }


def test_drop_removes_the_database_role_and_network_and_forgets_the_password(tmp_path: Path) -> None:
    docker = FakeDocker()
    provision_slot(docker, tmp_path)
    docker.calls.clear()

    provision.drop(config(), SERVICES, docker, workspace="slot2", state=tmp_path)

    sql = next(stdin for argv, stdin in docker.calls if "psql" in argv)
    assert sql == 'DROP DATABASE IF EXISTS "notes_slot2" WITH (FORCE);\nDROP ROLE IF EXISTS "notes_slot2";\n'
    assert docker.networks == {}
    assert not (tmp_path / "notes_slot2.password").exists()
