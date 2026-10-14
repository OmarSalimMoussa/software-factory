"""software-stack-run@<id>.service: one stack request as the stack user, with its rootless Docker.

Run as ``/usr/bin/python3 -I <install>/runtime/system/runners/stack_run.py <state> <id>``. A product verb runs the
checkout's own ``scripts/dev`` with its slot's facts in the environment. Before each one, the slot gets its share of
the shared services (its network, database and error projects) and its env file, written from the product's template;
a verb never runs on a slot half provisioned. After a successful ``empty``, the slot's database and env file go; what
``scripts/dev`` wrote beside the env file is the product's own to clear.
Three verbs are the factory's and never reach the product: sweeping a slot's labelled Docker objects (and the
containers still on its networks), the nightly prune, and Docker's disk use. Stdlib only.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

# `python3 -I` leaves the script's own folder off sys.path; the install's root is root-owned, so it is safe to add.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from runtime.shared import environment
from runtime.shared.paths import SLOT_KEYS, Places
from runtime.shared.settings import Settings
from runtime.slots import leases
from runtime.system import layout
from runtime.system.runners import launch_file as lf
from standard import config as product_config
from standard import envfile
from standard.config_records import Config
from standard.services import provision, stack

SLOT_LABEL = "software.slot"
RUN_LABEL = "software.run"
EMPTY = "empty"
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


def prune(hours: str, timeout: int) -> int:
    if not hours.isdigit() or int(hours) < 1:
        print("prune: name the age in whole hours", flush=True)
        return lf.REFUSED
    status = 0
    for args in (["builder", "prune", "-f"], ["image", "prune", "-a", "-f"]):
        done = _docker([*args, "--filter", f"until={hours}h"], timeout)
        print(done.stdout.strip() or done.stderr.strip(), flush=True)
        status |= done.returncode
    return 1 if status else 0


def events(since: str, until: str, timeout: int) -> int:
    if not (since.isdigit() and until.isdigit()):
        print("events: name the span in whole seconds since the epoch", flush=True)
        return lf.REFUSED
    span = ["--since", since, "--until", until, "--format", "{{json .}}", "--filter", "type=container"]
    wanted = [word for event in ("kill", "die", "oom") for word in ("--filter", f"event={event}")]
    done = _docker(["events", *span, "--filter", f"label={SLOT_LABEL}", *wanted], timeout)
    if done.returncode != 0:
        print(f"events: docker events failed: {done.stderr.strip()[-300:]}", flush=True)
        return 1
    for line in done.stdout.splitlines():
        raw = json.loads(line)
        actor = raw["Actor"]
        attributes = actor.get("Attributes", {})
        print(json.dumps({"action": raw["Action"], "at": raw["timeNano"], "id": actor["ID"][:12],
                          "name": attributes.get("name", ""), "slot": attributes.get(SLOT_LABEL, ""),
                          "exit": attributes.get("exitCode", "")}), flush=True)  # fmt: skip
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
    if verb[0] == leases.RUN and (why := _no_run(checkout, timeout)):
        print(f"stack-run: slot {slot} ran nothing: {why}", flush=True)
        return lf.REFUSED
    workspace, port = f"slot{slot}", leases.port(settings, slot)
    origin, commit = f"http://127.0.0.1:{port}", leases.stand_in(slot)
    env_dir, state = places.slot_env_dir(slot), places.slot_state_dir(slot)
    try:
        config = product_config.load(checkout)
        services = provision.read_services_env(places.services / stack.ENV_FILE)
        for folder in (env_dir, state):
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        values = provision.workspace(config, services, run, workspace=workspace, origin=origin, commit=commit,
                                     state=state)  # fmt: skip
        env_file = env_dir / config.env.file
        keys, given, notes = _slot_keys(places, config, values, env_file, checkout / config.env.template)
        placeholders = envfile.placeholder_values(workspace=workspace, origin=origin, port=port, commit=commit,
                                                  dir=checkout, env_dir=env_dir,
                                                  docker_socket=layout.rootless_socket(os.getuid()))  # fmt: skip
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
    label = f"{RUN_LABEL}={request}"
    env = {**environment.handed_on(), **leases.env(places, settings, slot, env_file), leases.RUN_LABEL_ENV: label}
    status = lf.run([str(checkout / "scripts" / "dev"), *verb], checkout, env, timeout)
    if verb[0] == leases.RUN and status == lf.TIMED_OUT:
        _stop_run(slot, label, timeout)
    held = envfile.placeholders(written)
    if held and verb[0] != EMPTY:
        print(f"stack-run: slot {slot} runs with a placeholder in {', '.join(held)}, so a call that needs one fails "
              "here; the owner gives slots their keys at install (slots.keys)", flush=True)  # fmt: skip
    if verb[0] != EMPTY or status != 0:
        return status
    try:
        provision.drop(config, services, run, workspace=workspace, state=state)
        env_file.unlink(missing_ok=True)
        envfile.defaults_path(env_file).unlink(missing_ok=True)
        _given_path(env_file).unlink(missing_ok=True)
    # telemetry: surfaced — what is left of the slot is `print`ed to the run's log, and the exit says so
    except (provision.ProvisionError, OSError) as exc:
        print(f"stack-run: slot {slot} is empty, but its database or env file is still there: {exc}", flush=True)
        return 1
    print(f"stack-run: slot {slot}'s database and env file are gone", flush=True)
    return 0


def _slot_keys(
    places: Places, config: Config, values: dict[str, str], env_file: Path, template: Path
) -> tuple[dict[str, str], list[str], list[str]]:
    """The owner's keys to write (software-slot-keys.service), their names, and a note on any the factory sets itself.
    A key the last write gave and this one does not takes the template's value, so a withdrawn key leaves the slot."""
    keys = places.services / SLOT_KEYS
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


def _no_run(checkout: Path, timeout: int) -> str:
    """Why the product cannot run a command in a slot; empty when its scripts/dev lists the optional run verb."""
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
    return "" if leases.RUN in done.stdout.split() else "this product's scripts/dev has no run verb"


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
    if verb[0] == lf.SWEEP:
        return sweep(slot, timeout)
    if verb[0] == lf.PRUNE:
        return prune(verb[1] if len(verb) == 2 else "", timeout)
    if verb[0] == lf.DISK:
        return disk(timeout)
    if verb[0] == lf.EVENTS:
        since, until = verb[1:3] if len(verb) == 3 else ("", "")
        return events(since, until, timeout)
    return None


def main(argv: list[str]) -> int:
    state, request_id = Path(argv[0]), argv[1]
    try:
        raw = lf.read(state, request_id, (lf.STACK,))
        settings, sessions = lf.published(state)
        slot, verb, session = lf.stack_parts(raw, settings)
        timeout = (
            settings.get_int("slots.run_seconds")
            if verb[0] == leases.RUN
            else settings.get_int("slots.verb_minutes") * 60
        )
        own = _own_verb(slot, verb, timeout)
        if own is not None:
            return own
        checkout = lf.checkout_for(session, settings, sessions)
    # telemetry: surfaced — the refusal is `print`ed to the unit's log, and the exit says it was refused
    except lf.BadRequest as exc:
        print(f"stack-run: refused: {exc}", flush=True)
        return lf.REFUSED
    # telemetry: surfaced — the timeout is `print`ed to the unit's log, and the exit says so
    except subprocess.TimeoutExpired:
        print("stack-run: docker did not answer in time", flush=True)
        return lf.TIMED_OUT
    places = Places.for_runner(Path(__file__).resolve().parents[3], state)
    return product_verb(
        places, settings, slot, verb, checkout, run=provision.real_run, timeout=timeout, request=request_id
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
