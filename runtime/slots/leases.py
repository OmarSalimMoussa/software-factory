"""Who holds which stack slot (factory spec, "Slots and stacks").

A lease is a small file in the slot's folder in the warden's state: ``checkout``, ``holder``, ``since`` and
``note``, one ``key=value`` line each. It is the one record of who holds a slot: the holder is a session's name. It
has no expiry, and no agent can take another's. Slot 0 is the main checkout's and never leased; slots 1 to
``slots.count`` are claimed one per implementer, under one lock. ``empty`` is the one way a slot is given up: the
holder's release, the orchestrator's free, a bench and a reap all use it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from runtime.shared import store
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused
from runtime.shared.settings import Settings
from runtime.warden.context import Context
from runtime.warden.verbs import SLOT_RUN

KEYS = ("holder", "since", "note")
# The stack unit's own verbs, beside the product's scripts/dev verbs. SWEEP removes every container, volume, network
# and image labelled with the request's slot, and every container still on one of its networks. PRUNE <hours> removes
# the build cache and the unused slot-labelled images older than that; DISK prints the bytes Docker's data takes,
# alone on its last line. RESTARTS prints how many times Docker restarted each container. TOP prints the CPU of each
# container running for the request's slot, and of each process in it.
SWEEP, PRUNE, DISK, RESTARTS = "factory-sweep-slot", "factory-prune", "factory-disk", "factory-restarts"
TOP = "factory-top"
# The product's optional slot verbs, which the stack unit runs only once `scripts/dev verbs` lists them: run <service>
# -- <command…> runs a command in a new container of the service; restart or stop <service…> restarts or stops them.
RUN, RESTART, STOP = SLOT_RUN, "restart", "stop"
OPTIONAL = (RUN, RESTART, STOP)
RUN_LABEL_ENV = "FACTORY_RUN_LABEL"


@dataclass(frozen=True, slots=True)
class Lease:
    slot: int
    holder: str
    since: str
    note: str


def _path(places: Places, n: int) -> Path:
    return places.slot(n) / "lease"


def read(places: Places, n: int) -> Lease | None:
    """Slot n's lease, or None when it is free; a lease that cannot be read raises StoreError."""
    try:
        text = _path(places, n).read_text()
    # telemetry: expected — no lease: the slot is free
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise store.StoreError(f"{_path(places, n)}: {exc}") from exc
    pairs = [line.partition("=") for line in text.splitlines()]
    values = {key: value for key, sep, value in pairs if sep}
    if [key for key, _, _ in pairs] != list(KEYS) or not values["holder"]:
        raise store.StoreError(f"{_path(places, n)}: a lease holds holder, since and note")
    return Lease(n, values["holder"], values["since"], values["note"])


def numbers(places: Places, settings: Settings) -> list[int]:
    """Every agent slot: 1 to slots.count, and any higher one still leased after the count was lowered."""
    count = settings.get_int("slots.count")
    extra = sorted(int(d.name) for d in places.slots.glob("*") if d.name.isdigit() and int(d.name) > count)
    return [*range(1, count + 1), *(n for n in extra if read(places, n) is not None)]


def held_by(places: Places, settings: Settings, holder: str) -> Lease | None:
    return next(
        (lease for n in numbers(places, settings) if (lease := read(places, n)) and lease.holder == holder), None
    )


def claim(ctx: Context, holder: str, note: str = "") -> int:
    """The slot ``holder`` holds, or the first free one, leased to it now; refused when every slot is held."""
    settings = ctx.settings()
    with store.locked(ctx.places.slots / ".lock"):
        mine = held_by(ctx.places, settings, holder)
        if mine is not None:
            return mine.slot
        for n in range(1, settings.get_int("slots.count") + 1):
            if read(ctx.places, n) is None:
                line = " ".join(note.split())
                text = f"holder={holder}\nsince={ctx.clock.iso()}\nnote={line}\n"
                store.write_text(_path(ctx.places, n), text, mode=0o600)
                return n
    raise Refused("slots-full", "every stack slot is held; ask the orchestrator to free a finished one")


def release(ctx: Context, n: int) -> None:
    with store.locked(ctx.places.slots / ".lock"):
        _path(ctx.places, n).unlink(missing_ok=True)


def empty(ctx: Context, n: int, session: str) -> str:
    """Give slot ``n`` up: the product's ``scripts/dev empty``, then every Docker object labelled with the slot, then
    the lease goes. "" when it is free; else what failed, with the lease kept so a later try finishes it."""
    for verb in (["empty"], [SWEEP]):
        done = ctx.launcher.run_stack(n, verb, session)
        if done.exit != 0:
            return f"{verb[0]} exited {done.exit}; see {done.log}"
    release(ctx, n)
    return ""


def port(settings: Settings, n: int) -> int:
    return settings.get_int("slots.port_base") + n


def origin(settings: Settings, n: int) -> str:
    """Where slot ``n``'s product says it is: staging (slot 0) at ``slots.zero_origin`` when set, else on loopback."""
    public = settings.get_str("slots.zero_origin")
    return public if n == 0 and public else f"http://127.0.0.1:{port(settings, n)}"


def stand_in(n: int) -> str:
    """The commit a slot's env file names: a slot's images carry this tag, and only the image knows its commit."""
    return f"dev-slot{n}"


def env(places: Places, settings: Settings, n: int, env_file: Path) -> dict[str, str]:
    """What the product's scripts/dev is told about its slot (factory spec, "scripts/dev"): ``env_file`` is the
    slot's env file, which the factory writes before each verb."""
    return {
        "FACTORY_SLOT": str(n),
        "FACTORY_PORT": str(port(settings, n)),
        "FACTORY_SLOT_DIR": str(places.slot(n)),
        "FACTORY_ENV_FILE": str(env_file),
    }
