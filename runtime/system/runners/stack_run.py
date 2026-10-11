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

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from runtime.shared.paths import Places  # noqa: E402 - the path line above must run first
from runtime.shared.settings import Settings  # noqa: E402
from runtime.slots import leases  # noqa: E402
from runtime.system import layout  # noqa: E402
from runtime.system.runners import launch_file as lf  # noqa: E402
from standard import config as product_config  # noqa: E402
from standard import envfile  # noqa: E402
from standard.services import provision, stack  # noqa: E402

SLOT_LABEL = "software.slot"
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
    except (ValueError, KeyError, TypeError) as exc:
        print(f"disk: docker system df printed something unreadable ({exc})", flush=True)
        return 1
    print(total, flush=True)
    return 0


def product_verb(places: Places, settings: Settings, slot: int, verb: list[str], checkout: Path, *,
                 run: provision.Run, timeout: int) -> int:  # fmt: skip
    """``scripts/dev <verb>`` on a slot provisioned first; its exit status, or 1 when provisioning or the drop after
    ``empty`` failed."""
    workspace, port = f"slot{slot}", leases.port(settings, slot)
    origin, commit = f"http://127.0.0.1:{port}", f"dev-slot{slot}"
    env_dir, state = places.slot_env_dir(slot), places.slot_state_dir(slot)
    # Everything this prints reaches agents and the evidence log: each secret it knows is hidden first, here, where
    # the values are, and not later from a file `empty` may already have removed.
    hidden: list[str] = []
    try:
        config = product_config.load(checkout)
        services = provision.read_services_env(places.services / stack.ENV_FILE)
        hidden += services.values()
        for folder in (env_dir, state):
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        values = provision.workspace(config, services, run, workspace=workspace, origin=origin, commit=commit,
                                     state=state)  # fmt: skip
        env_file = env_dir / config.env.file
        placeholders = envfile.placeholder_values(workspace=workspace, origin=origin, port=port, commit=commit,
                                                  dir=checkout, env_dir=env_dir,
                                                  docker_socket=layout.rootless_socket(os.getuid()))  # fmt: skip
        notes = envfile.write(config, template=checkout / config.env.template, target=env_file,
                              placeholders=placeholders, services=values, server=False)  # fmt: skip
        written, problems = envfile.parse_values(env_file.read_text(encoding="utf-8"), str(env_file))
        if problems:
            raise envfile.EnvError(problems)
        hidden += provision.secret_values(config, {**written, **values})
    except (product_config.ConfigError, provision.ProvisionError, envfile.EnvError, OSError, ValueError) as exc:
        said = provision.redact(str(exc), hidden)
        print(f"stack-run: slot {slot} could not be provisioned, so {verb[0]} did not run: {said}", flush=True)
        return 1
    for note in notes:
        print(f"stack-run: {note}", flush=True)
    env = {**os.environ, **leases.env(places, settings, slot, env_file)}
    status, said = lf.run_captured([str(checkout / "scripts" / "dev"), *verb], checkout, env, timeout)
    print(provision.redact(said, hidden), end="", flush=True)
    if verb[0] != EMPTY or status != 0:
        return status
    try:
        provision.drop(config, services, run, workspace=workspace, state=state)
        env_file.unlink(missing_ok=True)
        envfile.defaults_path(env_file).unlink(missing_ok=True)
    except (provision.ProvisionError, OSError) as exc:
        print(f"stack-run: slot {slot} is empty, but its database or env file is still there: {exc}", flush=True)
        return 1
    print(f"stack-run: slot {slot}'s database and env file are gone", flush=True)
    return 0


def main(argv: list[str]) -> int:
    state, request_id = Path(argv[0]), argv[1]
    try:
        raw = lf.read(state, request_id, (lf.STACK,))
        settings, sessions = lf.published(state)
        slot, verb, session = lf.stack_parts(raw, settings)
        timeout = settings.get_int("slots.verb_minutes") * 60
        if verb[0] == lf.SWEEP:
            return sweep(slot, timeout)
        if verb[0] == lf.PRUNE:
            return prune(verb[1] if len(verb) == 2 else "", timeout)
        if verb[0] == lf.DISK:
            return disk(timeout)
        checkout = lf.checkout_for(session, settings, sessions)
    except lf.BadRequest as exc:
        print(f"stack-run: refused: {exc}", flush=True)
        return lf.REFUSED
    except subprocess.TimeoutExpired:
        print("stack-run: docker did not answer in time", flush=True)
        return lf.TIMED_OUT
    places = Places.for_runner(Path(__file__).resolve().parents[3], state)
    return product_verb(places, settings, slot, verb, checkout, run=provision.real_run, timeout=timeout)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
