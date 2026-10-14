from __future__ import annotations

import stat
from pathlib import Path

import pytest
from deploy_kit import TEMPLATE, config

from standard import envfile
from standard.envfile import EnvError

PLACEHOLDERS = envfile.placeholder_values(
    workspace="prod",
    origin="https://notes.example.com",
    port=8300,
    commit="abcdef12",
    dir=Path("/srv/notes/prod/stack"),
    env_dir=Path("/srv/notes/prod/env"),
    docker_socket="/var/run/docker.sock",
)
SERVICES = {
    "DATABASE_URL": "postgresql://notes_prod:pw@factory-postgres:5432/notes_prod",
    "SENTRY_DSN_APP": "http://k@g/2",
}


def write(tmp_path: Path, template: str = TEMPLATE, *, server: bool = False) -> list[str]:
    (tmp_path / "notes.env.template").write_text(template)
    return envfile.write(
        config(),
        template=tmp_path / "notes.env.template",
        target=tmp_path / "env" / "notes.env",
        placeholders=PLACEHOLDERS,
        services=SERVICES,
        server=server,
    )


def values(tmp_path: Path) -> dict[str, str]:
    found, problems = envfile.parse_values((tmp_path / "env" / "notes.env").read_text(), "test")
    assert problems == []
    return found


def test_generated_values_are_filled_once_and_kept(tmp_path: Path) -> None:
    template = TEMPLATE + "STORE_KEY=pk-lf-generate-with-openssl-rand-hex-8\nFERNET=generate-with-python-fernet\n"
    write(tmp_path, template)
    first = values(tmp_path)
    write(tmp_path, template)

    assert values(tmp_path) == first
    assert len(first["SESSION_KEY"]) == 32 and first["SESSION_KEY"] != "generate-with-openssl-rand-hex-16"
    assert first["STORE_KEY"].startswith("pk-lf-") and len(first["STORE_KEY"]) == len("pk-lf-") + 16
    assert len(first["FERNET"]) == 44


def test_set_and_service_keys_are_rewritten_hand_values_kept_and_a_gone_key_dropped(tmp_path: Path) -> None:
    target = tmp_path / "env" / "notes.env"
    target.parent.mkdir()
    target.write_text(
        "NOTES_WORKSPACE=old\nNOTES_ORIGIN=http://old\nMAIL_HOST=mail.example.com\nDATABASE_URL=postgresql://old\n"
        "SENTRY_DSN_APP=http://old\nRETIRED=1\n"
    )

    notes = write(tmp_path)

    found = values(tmp_path)
    assert found["NOTES_WORKSPACE"] == "prod" and found["NOTES_ORIGIN"] == "https://notes.example.com"
    assert found["DATABASE_URL"] == SERVICES["DATABASE_URL"] and found["SENTRY_DSN_APP"] == SERVICES["SENTRY_DSN_APP"]
    assert found["MAIL_HOST"] == "mail.example.com"
    assert "RETIRED" not in found
    assert [n for n in notes if "RETIRED" in n] == ["dropped RETIRED: the template no longer has it"]
    text = target.read_text()
    assert text.startswith("# The notes app's env file.\n")
    assert text.endswith(f"{envfile.SERVICE_BLOCK}\nSENTRY_DSN_APP=http://k@g/2\n")


def test_a_changed_default_reaches_the_file_and_a_value_set_by_hand_stays(tmp_path: Path) -> None:
    write(tmp_path, TEMPLATE + "MODEL=model-a\nREVIEW_MODEL=model-a\nTOKEN=generate-with-openssl-rand-hex-8\n")
    target = tmp_path / "env" / "notes.env"
    target.write_text(target.read_text().replace("REVIEW_MODEL=model-a", "REVIEW_MODEL=model-hand"))
    token = values(tmp_path)["TOKEN"]

    notes = write(tmp_path, TEMPLATE + "MODEL=model-b\nREVIEW_MODEL=model-b\nTOKEN=generate-with-openssl-rand-hex-8\n")

    found = values(tmp_path)
    assert (found["MODEL"], found["REVIEW_MODEL"], found["TOKEN"]) == ("model-b", "model-hand", token)
    assert notes == ["updated MODEL: the template's default changed"]


def test_a_file_written_before_its_defaults_were_kept_keeps_every_value(tmp_path: Path) -> None:
    target = tmp_path / "env" / "notes.env"
    target.parent.mkdir()
    target.write_text("MAIL_HOST=mail.example.com\nMODEL=model-a\n")

    notes = write(tmp_path, TEMPLATE + "MODEL=model-b\n")

    assert values(tmp_path)["MODEL"] == "model-a"
    assert f"kept every value in {target}: no record of the template's defaults at its last write" in notes
    assert envfile.defaults_path(target).read_text().endswith("MODEL=model-b\n")


def test_a_server_refuses_every_marked_key_left_at_the_template_value(tmp_path: Path) -> None:
    template = TEMPLATE + "# server: the laptop's key signs nothing real\nSIGNING_KEY=laptop-key\n"

    with pytest.raises(EnvError) as refused:
        write(tmp_path, template, server=True)

    problems = refused.value.problems
    assert any(p.startswith("MAIL_HOST still holds the template's value") and "relays nothing" in p for p in problems)
    assert any(p.startswith("SIGNING_KEY still holds the template's value") and "signs nothing" in p for p in problems)
    assert values(tmp_path)["MAIL_HOST"] == "localhost"  # written, so the owner has a file to fill in
    write(tmp_path, template, server=False)  # a laptop slot keeps the laptop value


def test_a_server_refuses_a_placeholder_in_any_key(tmp_path: Path) -> None:
    template = TEMPLATE + "MODEL_KEY=REPLACE_ME\nAGENT_KEY='<agent-key>'\nMOTTO=<b>bold</b>\n"

    with pytest.raises(EnvError) as refused:
        write(tmp_path, template, server=True)

    named = {p.split(" ", 1)[0] for p in refused.value.problems if "placeholder" in p}
    assert named == {"MODEL_KEY", "AGENT_KEY"}
    assert not any("<agent-key>" in p for p in refused.value.problems)  # the refusal names keys, never values
    write(tmp_path, template, server=False)  # a laptop slot runs with a placeholder, and slot up names it


def test_the_file_is_private_and_replaced_whole(tmp_path: Path) -> None:
    write(tmp_path)
    write(tmp_path)

    target = tmp_path / "env" / "notes.env"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert sorted(p.name for p in target.parent.iterdir()) == ["notes.env", "notes.env.defaults"]


@pytest.mark.parametrize(
    ("line", "problem"),
    [
        ("export MAIL_PORT=25", "line 9 is not KEY=value"),
        ("# server: unexplained\n\nMAIL_PORT=25", "line 9: a `# server:` line must sit directly above"),
        ("MAIL_HOST=again", "line 9 sets MAIL_HOST a second time"),
        ("BIG=generate-with-openssl-rand-hex-0", "line 9: BIG asks for 1 to 256 random bytes"),
    ],
)
def test_a_malformed_template_is_refused_naming_its_line(tmp_path: Path, line: str, problem: str) -> None:
    with pytest.raises(EnvError) as refused:
        write(tmp_path, TEMPLATE + line + "\n")

    assert any(problem in p for p in refused.value.problems), refused.value.problems
    assert not (tmp_path / "env" / "notes.env").exists()
