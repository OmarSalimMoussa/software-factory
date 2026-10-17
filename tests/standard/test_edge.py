from __future__ import annotations

from pathlib import Path

from deploy_kit import config

from standard.server import edge


def test_the_site_answers_blocked_paths_first_and_keeps_websockets_through_a_reload(tmp_path: Path) -> None:
    edge.write_sites(config(), tmp_path, "staging")

    assert (tmp_path / "notes-staging.caddy").read_text() == (
        f"{edge.HEADER}\n"
        "staging.notes.example.com {\n"
        "\trespond /admin/export* 404\n"
        "\treverse_proxy 127.0.0.1:8301 {\n"
        "\t\tstream_close_delay 30s\n"
        "\t}\n"
        "}\n"
    )
    assert (tmp_path / "notes-routes.caddy").read_text() == (
        f"{edge.HEADER}\napi.notes.example.com {{\n\tredir https://notes.example.com{{uri}} permanent\n}}\n"
    )


def test_a_site_bound_to_one_address_listens_there_alone() -> None:
    text = edge.site(["http://203.0.113.7:8090"], 8100, bind="203.0.113.7")

    # A wildcard would collide with a site on the same port elsewhere, and answer on every bridge gateway too.
    assert text.splitlines()[1:3] == ["http://203.0.113.7:8090 {", "\tbind 203.0.113.7"]
