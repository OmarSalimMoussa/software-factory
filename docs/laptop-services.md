# The laptop's shared services

One stack of shared services runs on the owner's laptop, and every slot's stack uses it: Postgres for the slots'
databases, pgweb to browse them, Langfuse for traces and GlitchTip for errors. This page says where to open each one,
what each slot gets from them, and where their keys live.

## What runs them

The stack user, `software-stack`, runs the services on its own rootless Docker, as the service
`software-services.service`. A container's root is that user on the laptop, never root. The installer places the
stack, starts it and enables it, so it comes back after a restart. A reinstall leaves it running and applies a changed
stack; the data stays.

The factory's own GlitchTip runs apart, on the rootless Docker of a user of its own, `software-errors`, as the service
`software-errors.service`. It starts and stops on its own, whatever the shared services are doing.

- Is it up? `systemctl status software-services.service`. When it is not, its log names each service that did not
  become healthy.
- Restart it: `sudo systemctl restart software-services.service`. A stop keeps the containers and their data.
- At every install, one drill checks that GlitchTip and Langfuse answer the stack user on the addresses below, and
  another that the factory's own GlitchTip answers the warden and neither the agent user nor the stack user.

## Where to open them

Every address is on the laptop's loopback, so nothing outside the laptop reaches it.

| Service | Address | Sign in with |
| --- | --- | --- |
| GlitchTip: errors | http://127.0.0.1:8200 | The product's `[services].owner_email` and `GLITCHTIP_OWNER_PASSWORD` |
| Langfuse: traces | http://127.0.0.1:3100 | The same email and `LANGFUSE_INIT_USER_PASSWORD` |
| pgweb: the databases | http://127.0.0.1:8081 | Nothing: it opens as the databases' administrator |
| Postgres | 127.0.0.1:5434 | The user `postgres` and `POSTGRES_PASSWORD` |
| MinIO: Langfuse's files | http://127.0.0.1:9090 | Used by Langfuse; you never open it |
| The factory's own GlitchTip | http://127.0.0.1:8210 | The same email and `ERRORS_OWNER_PASSWORD` |

The passwords are in `/var/lib/software-factory-services/services.env`, which only root and the stack user can read,
and the factory's own GlitchTip's in `/var/lib/software-factory-errors/errors.env`, which only root and the errors user
can read:

```sh
sudo grep PASSWORD /var/lib/software-factory-services/services.env /var/lib/software-factory-errors/errors.env
```

The first install makes them. Every reinstall keeps them, because the databases were made with them.

## What each slot gets

Before every stack command on a slot (`up`, `down`, `health`, `sign-in` and the others), the factory makes sure the
slot has its share of the services, then writes the slot's env file. Each step finds what an earlier one made, so
the share is made once and then kept.

- **Its own database**, `<product>_slot<n>`, owned by its own database user of the same name. No other slot's user
  can connect to it.
- **Its own network**, `<product>-slot<n>-services`. It joins the shared Postgres, Langfuse and GlitchTip and
  nothing else, so two slots never share a network.
- **Its own GlitchTip projects**, `slot<n>-<name>` for each name in factory.toml's `[services].errors`. Slot 0
  runs master, so the warden files each new issue of its projects as a request, as it does prod's and staging's.
- **Its traces** in the product's one Langfuse project, under the environment `slot<n>`.
- **Its env file**, written from the product's env template. It holds the slot's own values: the workspace
  `slot<n>`, the origin `http://127.0.0.1:<port>` and the commit `dev-slot<n>`, plus the shared services' keys and
  your slot keys. `scripts/dev` finds it in `FACTORY_ENV_FILE`, beside `FACTORY_SLOT`, `FACTORY_PORT` and
  `FACTORY_SLOT_DIR`.

The env file and the database user's password live in the stack user's own folder, `/home/software-stack/slots/<n>/`,
which no other user can open. When the share cannot be made, the command does not run, and its output says why.

Every slot runs as the one stack user, with one Docker. So the code of any slot can read every slot's env file and
the shared services' keys. These are throwaway development values: never put a prod key, a personal account or
anything else you would not hand an agent into the env template's defaults, `[env.set]` or the shared services.

## Slot keys

A key the product needs from a person, such as a model provider's API key, comes from your own env file. Name the
file and the keys when you install, as `--set slots.keys_from="<full path>"` and `--set slots.keys=["<KEY>", …]`;
root reads them from its own record of the install, so nothing the factory runs can change them. Before each stack
command, `software-slot-keys.service` copies just those keys into `/var/lib/software-factory-services/slot-keys.env`,
and the slot's env file takes them from there. A key your file lacks, holds as a placeholder, or stops naming goes
back to the template's value at the slot's next command, and that command's output names it. A key the factory sets
for each slot stays the factory's. Name only keys you would hand an agent, such as one with a spending cap: any
slot's code can read them.

Releasing a slot runs the product's `empty`. When `empty` succeeds, the factory also drops the slot's database, its
database user and its network, and removes its env file. The slot's GlitchTip projects stay, with their history.

## The factory's own errors

The warden's and the dashboard's own errors go to the factory's own GlitchTip, a second GlitchTip apart from the
shared one: its project `factory`, in the environment `factory`, named by the installed commit. Its user's Docker is
no one else's, the firewall keeps the agent user and the stack user off its port, and no slot joins its network. The
installer saves no address for it but its own. `factory errors` reads only the product's GlitchTip, so an agent never
sees the factory's own errors.

The installer sets it up. Without its address, the warden and the dashboard do not start, and their logs say why.

## The keys the warden holds

The installer keeps these in the warden's secrets folder:

- `laptop-glitchtip.json`: this GlitchTip's address, its organization and a read-only token, for
  `factory errors --where slot <n>`. Only the warden reads it.
- `laptop-langfuse.json`: this Langfuse's address and the product project's keys, for
  `factory traces --where slot <n>`. Only the warden reads it.
- `factory-dsn`: the address of the factory's own GlitchTip, where the factory's own errors go. Only the warden
  and the dashboard read it.
- `server.json`: the server's GlitchTip and Langfuse keys, copied from `~/.config/software-factory/server.json`,
  which the server set-up writes. Only the warden reads it. Until the server set-up has run, the installer says so
  and the warden imports no server errors.
