"""software-stack-run@<id>.service: one stack request as the stack user, on this machine's own Docker.

Run as ``/usr/bin/python3 -I <install>/runtime/system/runners/stack_run.py <state> <id>``. A product verb runs the
checkout's own ``scripts/dev`` with its slot's facts in the environment. Before each one, the slot gets its share of
the shared services (its network, database and error projects) and its env file, written from the product's template;
a verb never runs on a slot half provisioned. An ``up`` that would change nothing runs nothing (same_up.py), nor does
a ``run`` in a service whose container commands this machine's Docker (docker_reach.py). After a successful
``empty``, the slot's database and env file go; what ``scripts/dev`` wrote beside the env file is the product's own to
clear.
The factory's own verbs never reach the product: sweeping a slot's labelled Docker objects (and the containers still
on its networks), the clean-up's prune of slots' images alone, Docker's disk use, and restart counts. Stdlib only.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

# `python3 -I` leaves the script's own folder off sys.path; the install's root is root-owned, so it is safe to add.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from runtime.shared import environment
from runtime.shared.paths import SLOT_KEYS, Places
from runtime.shared.settings import Settings
from runtime.slots import leases
from runtime.system.runners import docker_reach, same_up
from runtime.system.runners import launch_file as lf
from runtime.warden import public
from standard import config as product_config
from standard import envfile
from standard.config_records import Config
from standard.services import host_paths, provision

SLOT_LABEL = "software.slot"
RUN_LABEL = "software.run"
COMPOSE_PROJECT = "com.docker.compose.project"
# Only these fields, so nothing a container's settings hold (its environment's secrets) is ever printed.
_COUNT = '{"id":{{json .Id}},"name":{{json .Name}},"restarts":{{.RestartCount}},"labels":{{json .Config.Labels}}}'
_STATS = "{{.ID}} {{.Name}} {{.CPUPerc}} CPU, {{.MemUsage}}"
_MEMBERS = "{{range $id, $c := .Containers}}{{$id}} {{end}}"
EMPTY, UP = "empty", "up"
# The verbs that start a slot's containers; each then runs below prod's, so prod answers first on a busy machine.
_STARTS = frozenset({UP, "desktop", leases.RESTART})
# docker system df prints go-units sizes: decimal steps of 1000, such as 0B, 512kB, 1.25GB.
_SIZE = re.compile(r"^([\d.]+)\s*([kKMGTPEZY]?)B$")
_STEPS = "kmgtpezy"
# Each kind of Docker object a slot labels, with the commands that list and remove it.
_KINDS = (
    (["container", "ls", "-aq"], ["container", "rm", "-f"]),
    (["volume", "ls", "-q"], ["volume", "rm", "-f"]),
    (["network", "ls", "-q"], ["network", "rm"]),
    (["image", "ls", "-q"], ["image", "rm", "-f"]),
)


def _docker(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=False)


def _free(networks: list[str], slot: int, timeout: int) -> int:
    """Force-remove every container still on the slot's networks; the number of failures. What the product's services
    start at run time (agent containers) carries no slot label, and keeps a network it is on from being removed."""
    failed = 0
    for network in networks:
        found = _docker(["network", "inspect", "--format", "{{range .Containers}}{{.Name}} {{end}}", network], timeout)
        names = sorted(found.stdout.split())
        if found.returncode != 0:
            print(f"sweep: inspecting network {network} failed: {found.stderr.strip()[-300:]}", flush=True)
            failed += 1
        elif names:
            done = _docker(["container", "rm", "-f", *names], timeout)
            print(f"sweep: removed {len(names)} container(s) still on slot {slot}'s network {network}: "
                  f"{', '.join(names)}", flush=True)  # fmt: skip
            failed += done.returncode != 0
    return failed


def sweep(slot: int, timeout: int) -> int:
    failed = 0
    for listing, removal in _KINDS:
        found = _docker([*listing, "--filter", f"label={SLOT_LABEL}={slot}"], timeout)
        ids = sorted(set(found.stdout.split()))
        if found.returncode != 0:
            print(f"sweep: listing {listing[0]}s failed: {found.stderr.strip()[-300:]}", flush=True)
            failed += 1
        elif ids:
            if listing[0] == "network":
                failed += _free(ids, slot, timeout)
            done = _docker([*removal, *ids], timeout)
            print(f"sweep: removed {len(ids)} {listing[0]}(s) of slot {slot}", flush=True)
            failed += done.returncode != 0
    return 1 if failed else 0


def prune(images: str, cache: str, timeout: int) -> int:
    if not images.isdigit() or not cache.isdigit():
        print("prune: name the images' and the build cache's ages in whole hours", flush=True)
        return lf.REFUSED
    status = 0
    # Prod's images share this Docker: only an image a slot labelled is ever pruned, so prod can always push back.
    # --all reaches the cache an image shares too, most of it; without it the age reaches a small part.
    for args, hours in (
        (["builder", "prune", "--all", "-f"], cache),
        (["image", "prune", "-a", "-f", "--filter", f"label={SLOT_LABEL}"], images),
    ):
        done = _docker([*args, *(["--filter", f"until={hours}h"] if int(hours) else [])], timeout)
        print(done.stdout.strip() or done.stderr.strip(), flush=True)
        status |= done.returncode
    return 1 if status else 0


def restarts(timeout: int) -> int:
    """Each container's restart count, a `run`'s aside, one line each: Docker counts only a stop nobody asked for."""
    listed = _docker(["container", "ls", "-aq", "--no-trunc"], timeout)
    if listed.returncode != 0:
        print(f"restarts: listing the containers failed: {listed.stderr.strip()[-300:]}", flush=True)
        return 1
    ids = listed.stdout.split()
    if not ids:
        return 0
    done = _docker(["container", "inspect", "--format", _COUNT, *ids], timeout)
    if _failed(done):
        print(f"restarts: inspecting the containers failed: {done.stderr.strip()[-300:]}", flush=True)
        return 1
    for line in done.stdout.splitlines():
        raw = json.loads(line)
        labels = raw["labels"] or {}
        if RUN_LABEL in labels:  # a `run`'s exit is its command's, answered to whoever ran it
            continue
        found = {"id": raw["id"][:12], "name": raw["name"].lstrip("/"), "restarts": raw["restarts"]}
        print(json.dumps(found | {"slot": labels.get(SLOT_LABEL, ""), "project": labels.get(COMPOSE_PROJECT, "")}),
              flush=True)  # fmt: skip
    return 0


def top(slot: int, timeout: int) -> int:
    """Each container running for the slot, its own and those on its networks, then each process in it."""
    listed = _docker(["container", "ls", "-q", "--filter", f"label={SLOT_LABEL}={slot}"], timeout)
    networks = _docker(["network", "ls", "-q", "--filter", f"label={SLOT_LABEL}={slot}"], timeout)
    if listed.returncode != 0 or networks.returncode != 0:
        said = (listed.stderr + networks.stderr).strip()[-300:]
        print(f"top: listing slot {slot}'s containers failed: {said}", flush=True)
        return 1
    ids = {cid[:12] for cid in listed.stdout.split()}
    for network in networks.stdout.split():
        on = _docker(["network", "inspect", "--format", _MEMBERS, network], timeout)
        ids |= {cid[:12] for cid in on.stdout.split()}
    if not ids:
        print(f"top: slot {slot} runs no container", flush=True)
        return 0
    stats = _docker(["container", "stats", "--no-stream", "--format", _STATS, *sorted(ids)], timeout)
    if _failed(stats):
        print(f"top: reading slot {slot}'s containers' CPU failed: {stats.stderr.strip()[-300:]}", flush=True)
        return 1
    for line in sorted(stats.stdout.splitlines()):
        cid, _, said = line.partition(" ")
        print(said, flush=True)
        # Names alone, as a command line can carry a secret; docker top refuses columns without the pid.
        for row in _docker(["container", "top", cid, "-eo", "pid,pcpu,etime,comm"], timeout).stdout.splitlines()[1:]:
            print(f"  {' '.join(row.split())}", flush=True)
    return 0


def _failed(done: subprocess.CompletedProcess[str]) -> bool:
    """Whether a command on listed containers failed; one removed since the list is gone, which is no failure."""
    said = [line for line in done.stderr.splitlines() if line.strip()]
    return done.returncode != 0 and (not said or any("No such container" not in line for line in said))


def _below_prod(slot: int, verb: str, shares: int, timeout: int) -> int:
    if verb not in _STARTS:
        return 0
    equals = f"slot {slot}'s containers may share the CPU with prod's as equals"
    try:
        found = _docker(["container", "ls", "-q", "--filter", f"label={SLOT_LABEL}={slot}"], timeout)
        ids = found.stdout.split()
        update = ["container", "update", "--cpu-shares", str(shares), *ids]
        done = _docker(update, timeout) if found.returncode == 0 and ids else found
    # telemetry: surfaced — the timeout is `print`ed to the run's log, and the exit says so
    except subprocess.TimeoutExpired:
        print(f"stack-run: docker did not answer in time, so {equals}", flush=True)
        return 1
    if _failed(done):
        print(f"stack-run: {equals}: {done.stderr.strip()[-300:]}", flush=True)
        return 1
    print(f"stack-run: slot {slot}'s {len(ids)} running container(s) take {shares} CPU shares to prod's 1024 each",
          flush=True)  # fmt: skip
    return 0


def size_bytes(text: str) -> int:
    found = _SIZE.match(text.strip())
    if found is None:
        raise ValueError(f"{text!r} is not a size")
    unit = found.group(2).lower()
    step = _STEPS.index(unit) + 1 if unit else 0
    return round(float(found.group(1)) * float(1000**step))


def disk(timeout: int) -> int:
    done = _docker(["system", "df", "--format", "{{json .}}"], timeout)
    if done.returncode != 0:
        print(f"disk: docker system df failed: {done.stderr.strip()[-300:]}", flush=True)
        return 1
    try:
        total = sum(size_bytes(json.loads(line)["Size"]) for line in done.stdout.splitlines() if line.strip())
    # telemetry: surfaced — the unreadable answer is `print`ed to the unit's log, and the exit says so
    except (ValueError, KeyError, TypeError) as exc:
        print(f"disk: docker system df printed something unreadable ({exc})", flush=True)
        return 1
    print(total, flush=True)
    return 0


def product_verb(places: Places, settings: Settings, slot: int, verb: list[str], checkout: Path, *,
                 run: provision.Run, timeout: int, request: str) -> int:  # fmt: skip
    """``scripts/dev <verb>`` on a slot provisioned first, as stack request ``request``; its exit status, or 1 when
    provisioning or the drop after ``empty`` failed."""
    why = _lacks(checkout, verb[0], timeout) if verb[0] in leases.OPTIONAL else ""
    if verb[0] == leases.RUN and not why:
        why = docker_reach.run_refusal(f"{SLOT_LABEL}={slot}", verb[1], _docker, timeout)
    if why:
        print(f"stack-run: slot {slot} ran nothing: {why}", flush=True)
        return lf.REFUSED
    workspace, port = f"slot{slot}", leases.port(settings, slot)
    origin, commit = leases.origin(settings, slot), leases.stand_in(slot)
    env_dir, state = places.slot_env_dir(slot), places.slot_state_dir(slot)
    try:
        config = product_config.load(checkout)
        for folder in (env_dir, state):
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        values = provision.workspace(config, run, workspace=workspace, origin=origin, commit=commit, state=state)
        env_file = env_dir / config.env.file
        keys, given, notes = _slot_keys(places, config, values, env_file, checkout / config.env.template)
        placeholders = envfile.placeholder_values(workspace=workspace, origin=origin, port=port, commit=commit,
                                                  dir=checkout, env_dir=env_dir,
                                                  docker_socket=host_paths.DOCKER_SOCKET)  # fmt: skip
        notes += envfile.write(config, template=checkout / config.env.template, target=env_file,
                               placeholders=placeholders, services=values | keys, server=False)  # fmt: skip
        written, problems = envfile.parse_values(env_file.read_text(encoding="utf-8"), str(env_file))
        if problems:
            raise envfile.EnvError(problems)
        envfile.atomic_write(_given_path(env_file), "".join(f"{name}\n" for name in given), 0o600)
    # telemetry: surfaced — why the slot could not be provisioned is `print`ed to the run's log
    except (product_config.ConfigError, provision.ProvisionError, envfile.EnvError, OSError, ValueError) as exc:
        print(f"stack-run: slot {slot} could not be provisioned, so {verb[0]} did not run: {exc}", flush=True)
        return 1
    for note in notes:
        print(f"stack-run: {note}", flush=True)
    status = _dev(places, settings, slot, verb, checkout, env_file, timeout=timeout, request=request)
    held = envfile.placeholders(written)
    if held and verb[0] != EMPTY:
        print(f"stack-run: slot {slot} runs with a placeholder in {', '.join(held)}, so a call that needs one fails "
              "here; the owner gives slots their keys at install (slots.keys)", flush=True)  # fmt: skip
    if verb[0] != EMPTY or status != 0:
        return status
    try:
        provision.drop(config, run, workspace=workspace, state=state)
        env_file.unlink(missing_ok=True)
        envfile.defaults_path(env_file).unlink(missing_ok=True)
        _given_path(env_file).unlink(missing_ok=True)
    # telemetry: surfaced — what is left of the slot is `print`ed to the run's log, and the exit says so
    except (provision.ProvisionError, OSError) as exc:
        print(f"stack-run: slot {slot} is empty, but its database or env file is still there: {exc}", flush=True)
        return 1
    print(f"stack-run: slot {slot}'s database and env file are gone", flush=True)
    return 0


def _dev(places: Places, settings: Settings, slot: int, verb: list[str], checkout: Path, env_file: Path, *,
         timeout: int, request: str) -> int:  # fmt: skip
    """``scripts/dev <verb>`` on the provisioned slot, or nothing for an up that would change nothing; its exit."""
    same = (places.slot_state_dir(slot), f"label={SLOT_LABEL}={slot}", _docker, timeout)
    seconds = settings.get_int("slots.same_up_minutes") * 60 if verb[0] == UP else 0
    stamp, why = same_up.check(*same, checkout=checkout, env_file=env_file, verb=verb, seconds=seconds)
    if why:
        print(f"stack-run: slot {slot} ran nothing: {why}", flush=True)
        return 0
    label = f"{RUN_LABEL}={request}"
    env = {**environment.handed_on(), **leases.env(places, settings, slot, env_file), leases.RUN_LABEL_ENV: label}
    status = lf.run([str(checkout / "scripts" / "dev"), *verb], checkout, env, timeout)
    lowered = _below_prod(slot, verb[0], settings.get_int("slots.cpu_shares"), timeout)
    status = status or lowered
    if verb[0] == leases.RUN and status == lf.TIMED_OUT:
        _stop_run(slot, label, timeout)
    if stamp and status == 0:
        same_up.record(*same, stamp)
    return status


def _slot_keys(
    places: Places, config: Config, values: dict[str, str], env_file: Path, template: Path
) -> tuple[dict[str, str], list[str], list[str]]:
    """The owner's keys to write (software-slot-keys.service), their names, and a note on any the factory sets itself.
    A key the last write gave and this one does not takes the template's value, so a withdrawn key leaves the slot."""
    keys = places.keys / SLOT_KEYS
    copied = envfile.parse_values(keys.read_text(encoding="utf-8"), str(keys))[0] if keys.exists() else {}
    own = values.keys() | {key for key, _ in config.env.set}
    given = {name: value for name, value in copied.items() if name not in own}
    record = _given_path(env_file)
    before = record.read_text(encoding="utf-8").split() if record.exists() else []
    defaults = envfile.parse_values(template.read_text(encoding="utf-8"), str(template))[0]
    gone = [name for name in before if name in defaults and name not in given and name not in own]
    back = {name: envfile.generate(defaults[name]) or defaults[name] for name in gone}
    taken = sorted(copied.keys() & own)
    said = [f"slots.keys names {', '.join(taken)}, which the factory sets for each slot: the slot keeps its own"]
    return given | back, sorted(given), said if taken else []


def _given_path(env_file: Path) -> Path:
    return env_file.with_name(f"{env_file.name}.slot-keys")


def _lacks(checkout: Path, verb: str, timeout: int) -> str:
    """Why the product cannot run optional ``verb``; empty when its scripts/dev lists it."""
    try:
        done = subprocess.run(
            [str(checkout / "scripts" / "dev"), "verbs"],
            cwd=checkout,
            env=environment.handed_on(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    # telemetry: surfaced — the `exc` class is returned, for the caller to print to the run's log
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"scripts/dev verbs could not run ({type(exc).__name__})"
    if done.returncode != 0:
        return f"scripts/dev verbs exited {done.returncode}"
    return "" if verb in done.stdout.split() else f"this product's scripts/dev has no {verb} verb"


def _stop_run(slot: int, label: str, timeout: int) -> None:
    """Remove the container a run past its time left, by the label it was given: stopping scripts/dev stops none."""
    try:
        found = _docker(["container", "ls", "-aq", "--filter", f"label={label}"], timeout)
        ids = found.stdout.split()
        done = _docker(["container", "rm", "-f", *ids], timeout) if found.returncode == 0 and ids else found
    # telemetry: surfaced — what is left of the run is `print`ed to the run's log
    except subprocess.TimeoutExpired:
        print(f"stack-run: docker did not answer in time; the run's container may still be on slot {slot}", flush=True)
        return
    if done.returncode != 0:
        print(f"stack-run: the run's container may still be on slot {slot}: {done.stderr.strip()[-300:]}", flush=True)
    elif ids:
        print(f"stack-run: removed the run's {len(ids)} container(s) on slot {slot}", flush=True)
    else:
        print(f"stack-run: no container labelled {label} was found; the run's may still be on slot {slot}", flush=True)


def _own_verb(slot: int, verb: list[str], timeout: int) -> int | None:
    """The exit of one of the stack unit's own verbs; None for a product verb."""
    images, cache = verb[1:] if len(verb) == 3 else ["", ""]
    own: dict[str, Callable[[], int]] = {
        lf.SWEEP: lambda: sweep(slot, timeout),
        lf.PRUNE: lambda: prune(images, cache, timeout),
        lf.DISK: lambda: disk(timeout),
        lf.RESTARTS: lambda: restarts(timeout),
        lf.TOP: lambda: top(slot, timeout),
        lf.EXEC: lambda: docker_reach.exec_in(f"{SLOT_LABEL}={slot}", verb[1:], _docker, timeout),
    }
    return own[verb[0]]() if verb[0] in own else None


def main(argv: list[str]) -> int:
    state, request_id = Path(argv[0]), argv[1]
    places = Places.for_runner(Path(__file__).resolve().parents[3], state)
    try:
        raw = lf.read(state, request_id, (lf.STACK,))
        settings, sessions = lf.published(state)
        slot, verb, session = lf.stack_parts(raw, settings)
        run = verb[0] in (leases.RUN, lf.EXEC)
        timeout = settings.get_int("slots.run_seconds") if run else settings.get_int("slots.verb_minutes") * 60
        own = _own_verb(slot, verb, timeout)
        if own is not None:
            return own
        staging_at = slot == 0 and public.staging_at_path(places).exists()
        checkout = lf.checkout_for(session, settings, sessions, staging_at)
    # telemetry: surfaced — the refusal is `print`ed to the unit's log, and the exit says it was refused
    except lf.BadRequest as exc:
        print(f"stack-run: refused: {exc}", flush=True)
        return lf.REFUSED
    # telemetry: surfaced — the timeout is `print`ed to the unit's log, and the exit says so
    except subprocess.TimeoutExpired:
        print("stack-run: docker did not answer in time", flush=True)
        return lf.TIMED_OUT
    return product_verb(
        places, settings, slot, verb, checkout, run=provision.real_run, timeout=timeout, request=request_id
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
