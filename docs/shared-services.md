# The shared services

One copy of the shared services runs on the product's server, where the server set-up made it
([server.md](server.md)): Postgres for the databases, pgweb to browse them, and GlitchTip for errors. Prod, every
slot and the factory's own errors all use it. This page says what the factory's install needs
from it, what each slot gets, and where the keys live.

## What the install needs

The factory installs on that server, beside prod, and makes no services of its own.

- Before it changes anything, the install checks that `/srv/factory/services/services.env` is there and that
  `factory-postgres` and `factory-glitchtip` are running. When they are not, it stops and says to run the server
  set-up first.
- The stack user, `software-stack`, joins the `docker` group and runs every slot on the server's own Docker.
- `services.env` stays the admin's alone: a slot is provisioned inside the services' own containers, with no
  password. The uninstall leaves the services and their data as they are.

Slot code can use the server's Docker socket, so an agent's code can reach prod and its data: know that before a
product has real users.

## Where to open them

[server.md](server.md) lists their addresses on the tailnet. On the server itself, each answers on the loopback:
GlitchTip at http://127.0.0.1:8200, pgweb at http://127.0.0.1:8081 and Postgres at 127.0.0.1:5434. The passwords
are in `/srv/factory/services/services.env`.

## What each slot gets

Before every stack command on a slot (`up`, `down`, `health`, `sign-in` and the others), the factory makes sure the
slot has its share of the services, then writes the slot's env file. Each step finds what an earlier one made, so
the share is made once and then kept.

- **Its own database**, `<product>_slot<n>`, owned by its own database user of the same name. No other slot's user
  can connect to it.
- **Its own network**, `<product>-slot<n>-services`. It joins the shared Postgres and GlitchTip and nothing else,
  so two slots never share a network.
- **Its own GlitchTip projects**, `slot<n>-<name>` for each name in factory.toml's `[services].errors`. Slot 0
  runs the default branch as staging, so the warden files each new issue of its projects as a request, as it does
  prod's.
- **Its env file**, written from the product's env template. It holds the slot's own values: the workspace
  `slot<n>`, the origin `http://127.0.0.1:<port>` (slot 0's is `slots.zero_origin` when set), the commit `dev-slot<n>`
  and the Docker socket `/var/run/docker.sock`, plus the shared services' keys and your slot keys. `scripts/dev` finds
  it in `FACTORY_ENV_FILE`, beside `FACTORY_SLOT`, `FACTORY_PORT` and `FACTORY_SLOT_DIR`.

The env file and the database user's password live in the stack user's own folder, `/home/software-stack/slots/<n>/`,
which no other user can open. When the share cannot be made, the command does not run, and its output says why.

Every slot runs as the one stack user, on one Docker. So the code of any slot can read every slot's env file and
the shared services' keys. Never put a prod key, a personal account or anything else you would not hand an agent into
the env template's defaults, `[env.set]` or your slot keys.

Releasing a slot runs the product's `empty`. When `empty` succeeds, the factory also drops the slot's database, its
database user and its network, and removes its env file. The slot's GlitchTip projects stay, with their history.

Prod comes first on the CPU. Each time a slot's containers start (`up`, `restart`, `desktop`), the factory gives each
one `slots.cpu_shares` (64) against Docker's 1024 for each of prod's, so prod still answers on a busy server. A
container a slot's service starts by itself later keeps Docker's 1024.

The warden's nightly prune removes only images a slot labelled, never prod's, which Push back needs.
`gc.docker_budget_gib` counts all of the server's Docker data, prod's included.

## Slot keys

A key the product needs from a person, such as a model provider's API key, comes from an env file you name. Name the
file and the keys when you install, as `--set slots.keys_from="<full path>"` and `--set slots.keys=["<KEY>", …]`;
root reads them from its own record of the install, so nothing the factory runs can change them. Before each stack
command, `software-slot-keys.service` copies just those keys into `/var/lib/software-factory-keys/slot-keys.env`, and
the slot's env file takes them from there. A key your file lacks, holds as a placeholder, or stops naming goes back to
the template's value at the slot's next command, and that command's output names it. A key the factory sets for each
slot stays the factory's. Name only keys you would hand an agent, such as one with a spending cap: any slot's code can
read them.

## The factory's own errors

The warden's and the dashboard's own errors go to the project `factory` in the shared GlitchTip, in the product's
organization and the environment `factory`, named by the installed commit. The install makes the project as it makes
a slot's, and saves its address on the loopback. No workspace is named `factory`, so the warden never files these
issues as requests, and `factory errors` never shows them to an agent. Read them in GlitchTip, or through its MCP at
`/mcp`.

Without that address, the warden and the dashboard do not start, and their logs say why.

The warden reports two kinds of failure there too:

- **A container that stops when nothing asked:** a crash, or a kill from outside such as running out of memory.
  Every service of a slot and of prod restarts unless stopped, as does every shared service that keeps running, so
  Docker brings it back and counts it. Every `watch.container_deaths_minutes` the warden reads each container's
  count, and reports each count that rose. A stop the factory or a deploy asked for is never counted.
- **An import still failing after `watch.failing_alarm_minutes`:** prod's or staging's errors, or the users'
  reports. It is reported once for each run of failures.

## The keys the warden holds

The installer keeps these in the warden's secrets folder:

- `server.json`: the shared GlitchTip's address, organization and read-only token, and prod's report export with
  its token. The install copies it from `~/.config/software-factory/server.json`, which the server set-up writes on
  the machine it runs from. On the server itself, its addresses are loopback ones (`http://127.0.0.1:<port>`).
  `factory errors` and `factory logs` read every workspace through it, each slot's too. Only the warden reads it.
  Until the server set-up has run, the installer says so, and the warden imports no errors and no users' reports.
- `factory-dsn`: the address of the factory's GlitchTip project, where the factory's own errors go. Only the warden
  and the dashboard read it.
