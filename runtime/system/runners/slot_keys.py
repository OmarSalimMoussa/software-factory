"""The keys slots take from the owner's own env file, copied before each stack run (software-slot-keys.service).

The owner names the file (slots.keys_from) and the keys (slots.keys) at install, and this reads both from root's own
record of that install, never from the warden. Run as root, it reads the file in the owner's home, which no factory
user may, and writes only the named keys into the shared services' folder, where each slot's env write takes them. A
key the file lacks or holds as a placeholder is left out: the slot keeps the template's value and its stack commands
name it. A file that cannot be read leaves the last copy as it was. It prints key names, never a value.
"""

from __future__ import annotations

import grp
import os
import stat
import sys
import tomllib
from pathlib import Path

# `python3 -I` leaves the script's own folder off sys.path; the install's root is root-owned, so it is safe to add.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from runtime.shared import settings as settings_module
from runtime.shared import store, upgrades
from runtime.shared.paths import SLOT_KEYS, Places
from runtime.shared.settings import Settings
from runtime.system import layout
from standard import envfile


def _chosen(text: str, where: str, names: list[str]) -> tuple[dict[str, str], list[str]]:
    found, _ = envfile.parse_values(text, where)
    kept = {name: found[name] for name in names if found.get(name) and not envfile.placeholders({name: found[name]})}
    return kept, [name for name in names if name not in kept]


def _read(source: str) -> str:
    """The text of a plain file: a pipe or a device, which could keep the read waiting, raises OSError."""
    fd = os.open(source, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, encoding="utf-8") as file:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{source} is not a plain file")
        return file.read()


def copy(settings: Settings, target: Path, group: int) -> tuple[int, str]:
    """Write the named keys to ``target``, in ``group``: the exit status, and what to print."""
    source, names = settings.get_str("slots.keys_from"), settings.get_list("slots.keys")
    if not source or not names:
        target.unlink(missing_ok=True)
        return 0, "slot keys: none, since slots.keys_from or slots.keys is empty"
    try:
        kept, missing = _chosen(_read(source), source, names)
    # telemetry: surfaced — the unreadable file is what `said` prints to the unit's journal, and the exit says so
    except (OSError, UnicodeDecodeError) as exc:
        said = f"slot keys: {source} cannot be read ({type(exc).__name__}); the last copy stays"
        return 1, said
    envfile.atomic_write(target, "".join(f"{name}={value}\n" for name, value in kept.items()), 0o640, group)
    lacking = f"; {source} has no value for {', '.join(missing)}" if missing else ""
    return 0, f"slot keys: {', '.join(kept) or 'none'} copied{lacking}"


def installed(places: Places) -> Settings:
    record = upgrades.read_settings(places)
    if record is None:
        raise store.StoreError(f"{upgrades.settings_path(places)} is missing: install again")
    return settings_module.build(tomllib.loads(places.defaults.read_text(encoding="utf-8")), record)


def main(argv: list[str]) -> int:
    places = Places.for_runner(Path(__file__).resolve().parents[3], Path(argv[0]))
    try:
        settings = installed(places)
    # telemetry: surfaced — why the keys were not copied is `print`ed to the unit's journal, and the exit says so
    except (OSError, ValueError, store.StoreError, settings_module.SettingsError) as exc:
        print(f"slot keys: not copied: {exc}", flush=True)
        return 1
    status, said = copy(settings, places.services / SLOT_KEYS, grp.getgrnam(layout.STACK_USER).gr_gid)
    print(said, flush=True)
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
