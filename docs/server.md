# The server set-up

The set-up readies the product's one server for the standard deploy: prod's folders and settings, and the shared
services. You run it on your laptop, from a checkout of the factory, against the server named in the product's
`factory.toml`. It is safe to run again at any time: it changes only what differs, and says so.

```bash
python3 -m standard.server.setup --product <product checkout> --dry-run   # read everything, change nothing
python3 -m standard.server.setup --product <product checkout>             # do it
```

Options: `--fresh` also sets up the machine itself (a new server only). `--healthchecks-ping <url>` gives prod's
heartbeat its Healthchecks.io ping address. `--lockdown` only locks SSH down, as its own step.

## Before you start

- The server runs Ubuntu 24.04, whose Python is new enough (the set-up checks for 3.12 or newer), and you can sign in
  as the `[server] admin` user with your SSH key. That user needs sudo without a password. On a brand-new server,
  create it once as root: `adduser --disabled-password --gecos "" <admin>`, add your public key to `/home/<admin>/.ssh/authorized_keys`,
  and allow it sudo in `/etc/sudoers.d/<admin>` with `<admin> ALL=(ALL) NOPASSWD:ALL` (mode 0440).
- `[server] host_key` holds the server's SSH host key. The set-up trusts that key only, and stops if the server shows
  another. Read it from the provider's console, or with `ssh-keyscan -t ed25519 <host>` on a network you trust.
- The factory checkout has no uncommitted changes. The server runs exactly one commit of the factory.

## A new server: `--fresh`

On a new server, run the set-up with `--fresh`. It does what an existing server never gets from it:

1. The firewall: ufw allows 22, then 80 and 443, and only then is turned on, so it never locks you out.
2. A 4 GB swapfile, so running out of memory slows the server down instead of killing a database.
3. Docker and its compose plugin, from Docker's own package repository.
4. Tailscale, installed only. Joining the tailnet is your step: on the server, run
   `sudo tailscale up --hostname=<first part of [server] tailnet> --advertise-tags=tag:server`, then run the set-up
   again. The tag keeps the server apart from your own devices in the tailnet's rules, so the tailnet's policy must
   list `tag:server` under `tagOwners`. Joining with the tag also turns the server's key expiry off, so it never drops
   off the tailnet. The tailnet also needs MagicDNS and HTTPS certificates turned on in its DNS settings.

The set-up never installs a checkout of the product: prod's deploy builds each commit from the factory's own copy.

Lock SSH down last, as a separate run. First check from another terminal that `ssh <admin>@<host>` signs you in with
your key. Then run the set-up with `--lockdown`. SSH then takes keys only and refuses root. The set-up checks the
settings before SSH restarts, and takes them back out if SSH refuses them.

## An existing server

Without `--fresh`, the set-up only checks the machine and changes none of it:

| Check | It wants |
|---|---|
| Firewall | ufw on, allowing exactly 22, 80 and 443 inbound |
| SSH | passwords and keyboard sign-in off, root sign-in off |
| Tailscale | installed, running, and joined as `[server] tailnet` |
| Swap | some swap |
| Docker | Docker and its compose plugin |

If one fails, the set-up stops before it changes anything and says what to do.

## What the set-up places

- **The admin user.** It joins the docker group. Prod's deploys run as it ([deploy.md](deploy.md)).
- **The product's settings.** A copy of the product's `factory.toml` goes to `/etc/software-factory/product.toml`, a
  folder the factory's own install and uninstall leave alone. The admin user owns the file: each Push to prod makes it
  the pushed commit's `factory.toml`.
- **Two commands.** `factory-heartbeat` and `factory-gc` in `/usr/local/bin/`, for the two jobs below. Each runs
  the code of the factory installed on the server, in `/opt/software-factory/`.
- **Folders.** `/srv/factory/` for the shared services, the edge's site files and the deploy lock, and
  `/srv/<product>/<workspace>/` for each workspace's stack, env file and deploy records. The admin user owns them.
  The factory's install adds one site file of its own, `factory.caddy`, for its dashboard; a deploy never touches it.
- **The shared services.** Their compose file, the edge's settings and their own `services.env`, in
  `/srv/factory/services/`. The set-up brings them up and waits until every one is healthy. Then it creates
  GlitchTip's organization, your GlitchTip user and a read-only token.
- **Prod's heartbeat.** Every five minutes, as the admin user, the server asks prod's public health address and
  checks it answers as `prod`. Then it checks in with Healthchecks.io, or tells it at once that prod failed. When
  the check-ins stop, Healthchecks.io tells you, even when the server is down. Give the check's ping address once,
  with `--healthchecks-ping <url>`; it is kept in `/etc/software-factory/heartbeat-ping`.
- **The weekly clean-up.** Every Sunday at 04:17, as the admin user. It drops build cache older than a week and
  images no tag names, and logs one line to `/srv/factory/state/gc.log`. It never removes volumes or tagged images:
  volumes hold the databases and certificates, and tagged images are what a deploy or a rollback runs.
- **The tailnet.** The shared services' addresses below. With `--fresh` the set-up adds missing ones; otherwise it
  lists the commands to add them.
- **The warden's copy.** `~/.config/software-factory/server.json` on your laptop (only you can read it): the
  GlitchTip address, organization and read-only token, and the Langfuse address and keys. When `factory.toml` has
  `[reports]`, it also holds where prod's report export answers and its token, read from prod's env file; until prod
  is deployed, the set-up lists that as a step still to do. The factory's installer copies it for the warden.

## The shared services

One stack per server, compose project `factory-services`. Each workspace gets its own database, GlitchTip projects
and Langfuse environment from it at every deploy ([deploy.md](deploy.md)). Every port is bound to the server's
loopback address; only the tailnet reaches them, and only the edge answers on the internet (80 and 443, and
staging's port when the factory's `slots.zero_origin` names one).

| Service | Address on the tailnet |
|---|---|
| GlitchTip (errors and logs; its MCP endpoint is `/mcp`) | `https://<tailnet>:8200` |
| Langfuse (AI traces) | `https://<tailnet>:3100` |
| Langfuse's media store | `https://<tailnet>:9090` |
| pgweb (no sign-in of its own: the tailnet is its lock) | `https://<tailnet>:8081` |
| Postgres, for a desktop client (user `postgres`) | `<tailnet>:5434` |

Sign in to GlitchTip and Langfuse with `[services] owner_email`.

## Where the secrets live

- **On the server**, in `/srv/factory/services/services.env` (the admin user's; the factory's stack user's group may
  read it too, [shared-services.md](shared-services.md)): every shared service's passwords and keys, your GlitchTip
  password (`GLITCHTIP_OWNER_PASSWORD`), your Langfuse password (`LANGFUSE_INIT_USER_PASSWORD`) and the Postgres
  password (`POSTGRES_PASSWORD`). They are made once and kept: the databases were created with them. Read one with `ssh <admin>@<host> grep <KEY> /srv/factory/services/services.env`.
- **On your laptop**, `server.json`, in `~/.config/software-factory/`.

## Run it again

Run the set-up again, from a factory checkout at the right commit:

- **After a factory change** the product moves to. The set-up refuses a factory checkout at any other commit than
  the one the product's `.github/factory-ref` names, so move that first.
- **After a change to `factory.toml`** that touches what the set-up places: the server's folders, the shared
  services or the tailnet. Push to prod refreshes the server's copy of the file itself.
- **After a change to the shared services**, which comes with a factory change. The set-up brings them up again and
  joins them back to each workspace's networks.

Each run prints what it found and what it changed, and ends with "Changed nothing" when the server already matched.
