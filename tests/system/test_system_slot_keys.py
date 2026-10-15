from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from system_fakes import settings

from runtime.shared import store, upgrades
from runtime.shared.paths import Places
from runtime.system.runners import slot_keys

OWNER_FILE = "# the owner's own\nMODEL_KEY=sk-real\nSPARE_KEY=<your key>\nOTHER=kept-home\n"


@pytest.mark.parametrize(
    ("names", "status", "copied", "said"),
    [
        (["MODEL_KEY"], 0, "MODEL_KEY=sk-real\n", "slot keys: MODEL_KEY copied"),
        (
            ["MODEL_KEY", "SPARE_KEY", "GONE_KEY"],
            0,
            "MODEL_KEY=sk-real\n",
            "slot keys: MODEL_KEY copied; {source} has no value for SPARE_KEY, GONE_KEY",
        ),
        ([], 0, None, "slot keys: none, since slots.keys_from or slots.keys is empty"),
    ],
    ids=["named", "placeholder-and-missing-left-out", "none-named"],
)
def test_only_the_named_keys_with_a_real_value_leave_the_owners_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, names: list[str], status: int, copied: str | None, said: str
) -> None:
    source, target = tmp_path / "owner.env", tmp_path / "services" / "slot-keys.env"
    source.write_text(OWNER_FILE)
    target.parent.mkdir()
    target.write_text("STALE_KEY=old\n")

    groups: list[int] = []
    monkeypatch.setattr(os, "chown", lambda path, uid, gid: groups.append(gid))

    answer = slot_keys.copy(settings(**{"slots.keys_from": str(source), "slots.keys": names}), target, 4242)

    assert answer == (status, said.format(source=source))
    assert (target.read_text() if target.exists() else None) == copied
    assert (stat.S_IMODE(target.stat().st_mode), groups) == (0o640, [4242]) if copied else groups == []


@pytest.mark.parametrize(("kind", "error"), [("gone", "FileNotFoundError"), ("pipe", "OSError")])
def test_an_unreadable_owner_file_keeps_the_last_copy_and_names_no_value(tmp_path: Path, kind: str, error: str) -> None:
    target = tmp_path / "slot-keys.env"
    target.write_text("MODEL_KEY=sk-last\n")
    source = tmp_path / "owner.env"
    if kind == "pipe":
        os.mkfifo(source)  # a pipe no one writes to would keep the copy, and so every stack command, waiting

    answer = slot_keys.copy(settings(**{"slots.keys_from": str(source), "slots.keys": ["MODEL_KEY"]}), target, 0)

    assert answer == (1, f"slot keys: {source} cannot be read ({error}); the last copy stays")
    assert target.read_text() == "MODEL_KEY=sk-last\n"


def test_the_keys_come_from_roots_record_of_the_install_never_the_wardens_settings(tmp_path: Path) -> None:
    places = Places.under(tmp_path)
    places.install.mkdir(parents=True)
    places.defaults.write_text((Path(__file__).resolve().parents[2] / "settings.toml").read_text())
    with pytest.raises(store.StoreError, match="install again"):
        upgrades.installed_settings(places)
    record = {
        "repo.path": "/home/software-agent/shop",
        "repo.github": "acme/shop",
        "slots.keys": ["MODEL_KEY"],
        "slots.keys_from": "/home/owner/app.env",
    }
    upgrades.settings_path(places).parent.mkdir(parents=True)
    upgrades.settings_path(places).write_text(json.dumps(record))

    installed = upgrades.installed_settings(places)

    assert (installed.get_str("slots.keys_from"), installed.get_list("slots.keys")) == (
        "/home/owner/app.env",
        ["MODEL_KEY"],
    )
