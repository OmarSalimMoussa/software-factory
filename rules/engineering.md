# The rulebook

How code is built here, for any product. The product's `GOAL.md` says who it is for, how it looks and how it
speaks; this file says how to build it. The standard checks (`factory check`) enforce what they can. The rest is on
you, and on the critics and testers who hold you to it.

## Building

- **Fail loudly, never degrade silently.** Validate config at startup and raise one error that lists every problem.
  At runtime, raise a typed error instead of returning a partial or empty result. No fallback defaults: not `or 0`,
  not `unwrap_or(false)`, not a dash drawn for a value the server failed to send. Unknown stays unknown. A list
  draws an item it cannot read as a row that says so, reported once.
- **No backward compatibility before customers.** Replace, do not wrap: no old-format readers, field aliases,
  optional fields kept for old rows, or UI defaults for a field the server always sends. Delete the old data and
  the code that read it.
- **Design before building.** Split a large change into slices and verify each one end to end before you commit
  it. Do not overclaim: show what you ran and what it printed.
- **Own as little code as possible.** Every line is one someone must keep working. Before building, skip it, reuse
  what the product already has, or adopt a maintained library. No heavy infrastructure for a small need.
- **Permissive licences only** (MIT, BSD, Apache-2.0). No GPL, AGPL, SSPL or BSL. Read a dependency's licence
  before adding it.
- **Plain language, always (ISO 24495-1).** READMEs, docs, specs, commit messages and product copy lead with what
  the reader needs, in short sentences, active voice and common words, with no unexplained jargon.

## Telemetry

- **A platform failure reports.** A crashed or timed-out process, a store or network error, a misconfiguration:
  log a warning (handled) or an exception (unexpected) naming the ids involved and the failure class. Each new
  failure path gets one test that proves it logs.
- **A workflow failure surfaces.** A user's input that fails validation, a job that hits its own limit, a verdict
  the product reached on purpose: record it where the user sees it, draw it in the UI, and log nothing above info.
  No user content in a log message.
- **A failure only the browser sees reports too.** A stream or socket that never connects, a page that waits past
  its own limit: the page tells the server, which reports it once, with the ids and the failure class.
- **Report once, at the layer that knows.** Not in the service and again in the route.
- **A catch site re-raises, reports, or says why not** in a `telemetry:` comment (`expected`, `surfaced`,
  `last-resort`). The comment is a claim, not an exemption: never mark a platform failure to quiet the check.
- **Model calls record usage and cost on every call,** failed calls included. Unknown cost is recorded as unknown,
  never zero.

## The product's surface

- **The UI renders; the server computes.** Browser code never derives a value: no time conversion, URL building,
  position maths, vocabulary tables, summaries, counts or labels. It draws what the payload says. If a page needs a
  value the payload lacks, add it to the payload. Allowed in the browser: pixel layout from finished values, and
  display formatting of a number the server sent.
- **Outcomes, not machinery, in the voice `GOAL.md` sets.** The product is the actor in its copy. No step counts,
  ids, model names, stages or logs on the main surface; detail sits one click behind. Use the words `GOAL.md` uses,
  and never the ones it avoids.
- **One design system.** Every page uses the same stylesheets, tokens, type and icon set; no copy, no icon of your
  own, no colour written into a component rule. Every colour is a token with a light and a dark value.
- **Edges line up.** Controls share the edges of the content around them, one row shares one height, buttons come
  in a few size tiers, and every target is at least 24 by 24 px. Nothing scrolls sideways at phone width, except
  an element marked `data-scrolls-sideways` on purpose.

## Code

- **Thin routes.** A handler parses input, calls one service function and maps domain errors to status codes. The
  logic lives in a service module that tests call directly.
- **A composition root.** Dependencies are built in the entrypoint and passed in. A module never builds its own
  client or runner as a default.
- **No pass-throughs.** A function that only forwards to another goes; callers call the real one. Product code
  that only tests call is dead: delete it with its tests.
- **Search before you write a helper.** The second copy of an idiom moves to the shared module.
- **Comments say what the code cannot:** a constraint, a trap, a race, a wire byte, why a strange line is right. No
  comment that repeats the code, no history ("previously", "no longer"), no ticket narration.
- **Tests protect behaviour a caller can see.** Test through the public seam: the route, the service, the module's
  API. One test per rule; variants go in one parametrized table. No tests of getters, defaults or private helpers,
  or of what the type checker already proves. Keep a narrow test only for a trap: a boundary, an ordering, a
  security edge, a wire format, a cited bug. Unit tests stay offline and deterministic, each under 45 seconds.
- **Strict types are the floor.** No blanket ignores. An inline ignore names its code and says why.

## Structure

- **A module boundary is a folder and its README, never a barrel.** No `index.ts`, no re-exporting `__init__.py`.
  Importers name the module that owns the symbol.
- **Every folder has a README** saying what the part is and how it fits, with nothing technical about the code. A
  fact about the code lives in a comment at the constraint, or in `docs/`.
- **Size ceilings:** source 400 lines, tests 700, READMEs 100, other docs 200, module docblocks 20, and 25 files a
  folder. A file that outgrows one reader's head splits by concern into a subfolder. Growing past a ceiling needs a
  pin with its grounds in `check-values/baseline.json`.
- **A split is a move, not a rewrite.** The unchanged test suite is the proof. If an assertion needs editing, the
  refactor changed behaviour: stop and find out why.
- **Living docs hold what is true now:** no history, and no counts such as "all 12".

## Configuration and contracts

- **One validated config per service, loaded once at startup:** every key required, no defaults, every problem
  listed at once. The environment is read only in the config module, through one shared reader. A designed absence
  is an explicit optional read, never `get(key, default)`.
- **A literal shared across components lives once,** in one contracts file, generated into each language's copy.
  A file that cannot import the generated copy keeps a hand copy marked "must mirror", and a check pins the two.

## Data and shipping

- **Migrations keep the previous build working.** Prod can be pushed back to the commit it ran before, so a
  migration must leave that build able to run: add first, remove in a later change. A migration that cannot run
  backwards is fixed forward.
- **What a person reaches works from where they are.** A server's address, port or stream that answers only on the
  server itself, such as loopback or a port published on 127.0.0.1, is broken for every person. A server workspace
  sets its public address by hand, and a scenario checks the address a page is offered, not only that it loads.
- **Images carry their inputs.** A deploy builds them on the server from a clean copy of the commit, never at
  `docker compose up`.
