# Watching the factory

Four watches run as Claude Code Monitor commands. A Monitor ends after at most 30 minutes and says so: start it
again each time. Each one runs a script of [scripts/](scripts/README.md) on the server over SSH.

| Watch | Script, on the server | Every | Needs you when |
|---|---|---|---|
| Self-reflect and errors | `python3 -I $FACTORY_SCRIPTS/watch.py` | 2 min | it prints anything |
| Pulse | `python3 -I $FACTORY_SCRIPTS/pulse.py` | 5 min, printed on change | paused, staging down, merges amber, orchestrator not working |
| CPU | `sudo python3 -I $FACTORY_SCRIPTS/cpu_sample.py` | 4 min | the average stays above the target |
| Limits | `sudo python3 -I $FACTORY_SCRIPTS/limit_watch.py` | 3 min | it prints a limit |

Run `watch.py` once by hand before its watch starts: a first run prints every entry the factory has ever filed, and
remembers them, so the watch then prints only new ones. While the factory is paused, keep the Self-reflect watch, and
slow the CPU one: nothing else moves.

## The command

The same shape serves every watch. It masks anything that looks like a key or a sign-in link before it prints,
since a Monitor's lines land in the chat, and it says when SSH fails instead of going quiet:

```sh
while true; do
  out=$(timeout 120 ssh -o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=10 "$FACTORY_SSH" \
    "python3 -I $FACTORY_SCRIPTS/watch.py" 2>&1)
  code=$?
  if [ $code -ne 0 ]; then echo "watch: ssh failed (exit $code)"
  else echo "$out" | sed -E 's#auth/link[^ ]*#<link>#g; s/[A-Za-z0-9_-]{20,}/<masked>/g' | grep .; fi
  sleep 120
done
```

For the pulse, keep the last line in a variable and print only a new one. The CPU script samples for a full minute,
so give its `timeout` more than that.

## Reading what they print

- **`reflect <time> <kind> <id>: <text>`** is a new Self-reflect entry: a miss, friction, a discovery, a misroute or
  an incident. Read the whole entry (`snapshot reflections`, or `reflections.json` in the warden's state) and triage
  it now. An incident that says the warden paused the factory is the owner's to lift.
- **`error <id> x<count>: <title>`** is a new issue in the factory's own GlitchTip project: a fault of the factory's
  code. Find its cause in the warden's journal (`journalctl -u software-warden`) and fix it.
- **The pulse line** says, in order: running or paused (with the reason), the merges waiting for their check, the
  commit staging runs (DOWN when its last probe failed), testers at work, the orchestrator's state, and the plan's
  weekly use. During an install the orchestrator reads `bench-pending`, then `reviving`, then `working`.
- **The CPU line**:
  - `60s busy` and `steal` are the last minute;
  - `avg` is the warden's own moving average, the figure the owner's target is about;
  - `outside` is the cores used outside the factory's slice: the warden, Docker, the stacks and their browsers;
  - `factory cap` is the slice's limit, which the warden lowers as `outside` rises;
  - `load` is the run queue.

## The CPU budget

The machine is a VPS whose host meters CPU on credits. Past a baseline share the credits drain, and then the host
throttles the whole machine, which shows as steal. The owner wants the machine's average kept near that baseline.
The warden holds it there itself (`budget.cpu_target_percent`): it caps the factory's slice by what runs outside.
Past `budget.walk_hold_percent`, new browser testers wait. Past `vitals.cpu_red_percent`, nothing new starts.

When `avg` stays above the target, find what runs outside the slice:

```sh
top -b -n 2 -d 8 -o %CPU | awk '/^top -/{n++} n==2' | sed -n 8,20p
```

Then map each busy pid to its cgroup (`/proc/<pid>/cgroup`) and, for a container, to its name with `docker ps`.
The warden itself runs outside the slice: measure it over a minute from `/proc/<pid>/stat`, not from one `top`
sample. Judge steal over hours with `sar -u`, never from one short sample.

## The plan's weekly use

The warden pauses the factory once the plan's weekly use reaches `cost.plan_pause_percent`, says why on Home, files
one Self-reflect incident, and waits. Only the owner resumes it. Never switch the agents' account to get past a limit.
`snapshot plan` reads the use, and it reads `stale` while the orchestrator is paused, since the orchestrator's status
line is where the reading comes from. A limit error the watch marks WEEKLY means the same pause; a shorter window's
limit is worth telling the owner, not a pause.

## Merges and staging

Merges wait on staging for their regression check before they ship. The pulse's merge reading goes amber when too
many wait, or wait too long. A check that keeps failing on one scenario is most often a scenario a tester misreads:
read the tester's Self-reflect entries and the scenario before you blame the product.
