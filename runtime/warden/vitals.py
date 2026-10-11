"""The laptop's vitals, read every tick: memory, CPU pressure, power, the install, the git hooks, the agent's Claude
daemon and the firewall. Each reading is green, amber, red or unknown; unknown is treated as amber, never as fine.
A reading this machine does not have (a kernel without CPU pressure) is missing: shown as such, and it blocks
nothing. A machine with no battery listed, a desktop or a VM, is on mains power.

The permits read vitals.json: red blocks every start; amber blocks all but severe work. Readers take the kernel's
folders as arguments, so tests feed samples.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from runtime.shared import store
from runtime.shared.paths import AGENT_USER, SLICE, Places
from runtime.shared.settings import Settings
from runtime.warden import backstop, launch
from runtime.warden.context import Context

GREEN, AMBER, UNKNOWN, RED, MISSING = "green", "amber", "unknown", "red", "missing"
RECORDS = "sessions"
LEVELS = (GREEN, MISSING, AMBER, UNKNOWN, RED)  # worst last
PROC = Path("/proc")
POWER = Path("/sys/class/power_supply")
_PRESSURE = re.compile(r"^(some|full)\s+avg10=([\d.]+)\s+avg60=([\d.]+)", re.MULTILINE)
_GIB_KB = 1024 * 1024


@dataclass(frozen=True, slots=True)
class Reading:
    name: str
    level: str
    value: str  # in words, for factory status
    # The reading's number, for the dashboard: free memory in GiB, CPU pressure and battery charge in percent; None
    # for a reading without one, one not read, and the power of a machine on mains.
    figure: float | None = None


def worst(readings: list[Reading]) -> str:
    """The machine's level: the worst reading's, where a missing one counts for nothing."""
    return max((r.level for r in readings if r.level != MISSING), key=LEVELS.index, default=GREEN)


def memory(meminfo: str, settings: Settings) -> Reading:
    found = re.search(r"^MemAvailable:\s+(\d+) kB", meminfo, re.MULTILINE)
    if found is None:
        return Reading("memory", UNKNOWN, "cannot read the free memory")
    free = int(found.group(1)) / _GIB_KB
    value = f"{free:.1f} GiB free"
    if free < settings.get_float("vitals.memory_red_gib"):
        return Reading("memory", RED, value, free)
    if free < settings.get_float("vitals.memory_amber_gib"):
        return Reading("memory", AMBER, value, free)
    return Reading("memory", GREEN, value, free)


def cpu(pressure: str | None, settings: Settings) -> Reading:
    """``pressure`` is None when the kernel has no pressure file at all."""
    if pressure is None:
        return Reading("cpu", MISSING, "this kernel reports no CPU pressure")
    psi = {kind: float(avg60) for kind, _, avg60 in _PRESSURE.findall(pressure)}
    if "some" not in psi:
        return Reading("cpu", UNKNOWN, "cannot read the CPU pressure")
    level = AMBER if psi["some"] > settings.get_float("vitals.cpu_pressure_amber") else GREEN
    return Reading("cpu", level, f"pressure {psi['some']:.0f}% over a minute", psi["some"])


def power(supplies: Path, settings: Settings) -> Reading:
    on_mains, on_battery, battery = False, False, None
    try:
        listed = list(supplies.iterdir()) if supplies.exists() else []
        for supply in listed:
            # A mouse's or a headset's own battery says nothing about the machine's power.
            if (supply / "scope").exists() and (supply / "scope").read_text().strip() == "Device":
                continue
            kind = (supply / "type").read_text().strip()
            if kind == "Mains" and (supply / "online").read_text().strip() == "1":
                on_mains = True
            elif kind == "Battery":
                on_battery = True
                if (supply / "capacity").exists():
                    battery = int((supply / "capacity").read_text().strip())
    except (OSError, ValueError) as exc:
        return Reading("power", UNKNOWN, f"cannot read the power supplies: {type(exc).__name__}")
    if on_mains:
        return Reading("power", GREEN, "on mains power")
    if not on_battery:
        return Reading("power", GREEN, "no battery is listed, so on mains power")
    if battery is None:
        return Reading("power", AMBER, "on battery")
    level = RED if battery < settings.get_int("vitals.battery_red") else AMBER
    return Reading("power", level, f"on battery, {battery}%", float(battery))


def _files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts)


def install_digest(root: Path) -> str:
    """The hash the installer records and the warden checks: every file's path and bytes, in order."""
    digest = hashlib.sha256()
    for path in _files(root):
        digest.update(str(path.relative_to(root)).encode() + b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


# The install's file stamps and the hash they were read at, by install folder: the hash is read again only when a
# file's inode, size or change time moved, not every tick.
_HASHED: dict[str, tuple[str, str]] = {}


def _hash_when_changed(root: Path) -> str:
    stamps = [(str(p), st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns) for p in _files(root)
              for st in (p.stat(),)]  # fmt: skip
    stamp = hashlib.sha256(repr(stamps).encode()).hexdigest()
    seen = _HASHED.get(str(root))
    if seen is None or seen[0] != stamp:
        seen = _HASHED[str(root)] = (stamp, install_digest(root))
    return seen[1]


def install(places: Places) -> Reading:
    try:
        recorded = places.install_hash.read_text().strip()
        actual = _hash_when_changed(places.install)
    except OSError as exc:
        return Reading("install", UNKNOWN, f"cannot check the install: {type(exc).__name__}")
    if actual != recorded:
        return Reading("install", RED, "the install differs from what the installer recorded: reinstall")
    return Reading("install", GREEN, f"install {recorded[:12]}")


def hooks(places: Places, repo: Path, timeout: float) -> Reading:
    """Whether the product's checkout still runs the factory's git hooks, where the commit and push checks live."""
    want = str(places.install / "checks" / "git-hooks")
    try:
        done = subprocess.run(
            ["git", "config", "--includes", "--file", str(repo / ".git" / "config"), "--get", "core.hooksPath"],
            capture_output=True, text=True, timeout=timeout, check=False,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Reading("hooks", UNKNOWN, f"cannot read the checkout's git config: {type(exc).__name__}")
    got = done.stdout.strip()
    if done.returncode not in (0, 1):  # 1: the key is unset
        return Reading("hooks", UNKNOWN, f"cannot read the checkout's git config: exit {done.returncode}")
    if got != want:
        return Reading("hooks", RED, f"git takes its hooks from {got or 'its default folder'}, not {want}")
    return Reading("hooks", GREEN, "the checkout runs the factory's hooks")


def daemon(proc: Path, agent_uid: int | None) -> Reading:
    """The agent's Claude daemon, which hosts every background session, must run inside the factory's slice."""
    if agent_uid is None:
        return Reading("daemon", UNKNOWN, f"there is no {AGENT_USER} user")
    found, outside = False, []
    try:
        entries = [e for e in proc.iterdir() if e.name.isdigit()]
    except OSError as exc:
        return Reading("daemon", UNKNOWN, f"cannot list processes: {type(exc).__name__}")
    for entry in entries:
        try:
            if entry.stat().st_uid != agent_uid:
                continue
            argv = (entry / "cmdline").read_bytes().split(b"\0")
            if len(argv) < 3 or not argv[0].endswith(b"claude") or argv[1:3] != [b"daemon", b"run"]:
                continue
            cgroup = (entry / "cgroup").read_text()
        except OSError:  # the process ended while it was read
            continue
        found = True
        if f"/{SLICE}/" not in cgroup:
            outside.append(entry.name)
    if outside:
        return Reading("daemon", RED, f"the Claude daemon (pid {', '.join(outside)}) runs outside the factory's slice")
    if not found:
        return Reading("daemon", AMBER, "the agent's Claude daemon is not running")
    return Reading("daemon", GREEN, "inside the factory's slice")


def firewall(stamp: Path, now: float, settings: Settings) -> Reading:
    """The firewall table that walls the agent and stack users off the dashboard and the tailnet loads again every
    install.firewall_refresh_seconds, and each load touches ``stamp``: a stale stamp means it is not loading."""
    try:
        age = now - stamp.stat().st_mtime
    except FileNotFoundError:
        return Reading("firewall", RED, "the firewall has never loaded")
    except OSError as exc:
        return Reading("firewall", UNKNOWN, f"cannot read when the firewall last loaded: {type(exc).__name__}")
    if age > settings.get_int("vitals.firewall_stale_seconds"):
        return Reading("firewall", RED, f"the firewall last loaded {age:.0f} s ago")
    return Reading("firewall", GREEN, "loaded")


def records(proc: Path, agent_uid: int | None, settled: float) -> Reading:
    """Whether every Claude session the agent runs has the record the warden sees it by. One without blinds the
    backstop, the crash watch and the callers' roles at once, so it is red, and the crash watch waits."""
    if agent_uid is None:
        return Reading(RECORDS, UNKNOWN, f"there is no {AGENT_USER} user")
    try:
        found = backstop.unseen(backstop.sessions_folder(), proc, agent_uid, settled)
    except (OSError, ValueError, IndexError) as exc:
        return Reading(RECORDS, UNKNOWN, f"cannot list the agent's sessions: {type(exc).__name__}")
    if found:
        return Reading(RECORDS, RED, f"{len(found)} Claude sessions run with no session record (pid "
                                     f"{', '.join(map(str, found))}): the factory cannot see them, so it resumes and "
                                     "retires none; check the Claude Code version")  # fmt: skip
    return Reading(RECORDS, GREEN, "every running session has its record")


def blind(places: Places) -> bool:
    """Whether the last vitals found sessions the warden cannot see."""
    try:
        snapshot = store.read_json(places.vitals)
    except FileNotFoundError:
        return False
    found = [r for r in snapshot.get("readings", []) if r.get("name") == RECORDS] if isinstance(snapshot, dict) else []
    return any(r.get("level") == RED for r in found)


def _text(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:  # the reader reports unknown for empty text
        return ""


def _pressure(path: Path) -> str | None:
    """The pressure file's text; None when the kernel has none, "" (unknown) when it cannot be read."""
    return _text(path) if path.exists() else None


def refresh(ctx: Context, proc: Path = PROC, supplies: Path = POWER) -> dict[str, Any]:
    """Read every vital and write vitals.json for the permits, the dashboard and the status."""
    settings = ctx.settings()
    readings = [
        memory(_text(proc / "meminfo"), settings),
        cpu(_pressure(proc / "pressure" / "cpu"), settings),
        power(supplies, settings),
        install(ctx.places),
        hooks(ctx.places, Path(settings.get_str("repo.path")), settings.get_int("watch.tick_seconds")),
        daemon(proc, launch.agent_uid()),
        records(proc, launch.agent_uid(), settings.get_int("watch.start_grace_seconds")),
        firewall(ctx.places.firewall_stamp, ctx.clock.now(), settings),
    ]
    snapshot = {"at": ctx.clock.iso(), "level": worst(readings), "readings": [asdict(r) for r in readings]}
    store.write_json(ctx.places.vitals, snapshot)
    return snapshot
