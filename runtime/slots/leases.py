"""Who holds which stack slot (factory spec, "Slots and stacks").

A lease is a small file in the slot's folder in the warden's state: ``checkout``, ``holder``, ``since`` and
``note``, one ``key=value`` line each. It has no expiry, and no agent can take another's. Slot 0 is the main
checkout's and never leased; slots 1 to ``slots.count`` are claimed one per implementer, under one lock.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from runtime.shared import store
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused
from runtime.shared.settings import Settings
from runtime.warden.context import Context

KEYS = ("checkout", "holder", "since", "note")


@dataclass(frozen=True, slots=True)
class Lease:
    slot: int
    checkout: str
    holder: str
    since: str
    note: str


def _path(places: Places, n: int) -> Path:
    return places.slot(n) / "lease"


def read(places: Places, n: int) -> Lease | None:
    """Slot n's lease, or None when it is free; a lease that cannot be read raises StoreError."""
    try:
        text = _path(places, n).read_text()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise store.StoreError(f"{_path(places, n)}: {exc}") from exc
    pairs = [line.partition("=") for line in text.splitlines()]
    values = {key: value for key, sep, value in pairs if sep}
    if [key for key, _, _ in pairs] != list(KEYS) or not values["checkout"] or not values["holder"]:
        raise store.StoreError(f"{_path(places, n)}: a lease holds checkout, holder, since and note")
    return Lease(n, values["checkout"], values["holder"], values["since"], values["note"])


def numbers(places: Places, settings: Settings) -> list[int]:
    """Every agent slot: 1 to slots.count, and any higher one still leased after the count was lowered."""
    count = settings.get_int("slots.count")
    extra = sorted(int(d.name) for d in places.slots.glob("*") if d.name.isdigit() and int(d.name) > count)
    return [*range(1, count + 1), *(n for n in extra if read(places, n) is not None)]


def held_by(places: Places, settings: Settings, holder: str) -> Lease | None:
    return next(
        (lease for n in numbers(places, settings) if (lease := read(places, n)) and lease.holder == holder), None
    )


def claim(ctx: Context, holder: str, checkout: str, note: str = "") -> int:
    """The slot ``holder`` holds, or the first free one, leased to it now; refused when every slot is held."""
    settings = ctx.settings()
    with store.locked(ctx.places.slots / ".lock"):
        mine = held_by(ctx.places, settings, holder)
        if mine is not None:
            return mine.slot
        for n in range(1, settings.get_int("slots.count") + 1):
            if read(ctx.places, n) is None:
                line = " ".join(note.split())
                text = f"checkout={checkout}\nholder={holder}\nsince={ctx.clock.iso()}\nnote={line}\n"
                store.write_text(_path(ctx.places, n), text, mode=0o600)
                return n
    raise Refused("slots-full", "every stack slot is held; ask the orchestrator to free a finished one")


def release(ctx: Context, n: int) -> None:
    with store.locked(ctx.places.slots / ".lock"):
        _path(ctx.places, n).unlink(missing_ok=True)


def port(settings: Settings, n: int) -> int:
    return settings.get_int("slots.port_base") + n


def env(places: Places, settings: Settings, n: int) -> dict[str, str]:
    """What the product's scripts/dev is told about its slot (factory spec, "scripts/dev")."""
    return {"FACTORY_SLOT": str(n), "FACTORY_PORT": str(port(settings, n)), "FACTORY_SLOT_DIR": str(places.slot(n))}
