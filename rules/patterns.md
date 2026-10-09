# Patterns

Patterns that proved themselves in real products and that a second place may need. Each gives the rule, the reason,
and when to skip it. Scan the titles before you build something new: the problem may already be solved. When a
product lands a reusable pattern of its own, it records it in its own `docs/`.

## Processes and the network

**One network door.** Every outbound call goes through one module with two doors: a transport door that retries
idempotent methods, and an explicit door that wraps one attempt of anything else. Both retry only transient causes,
a few times, with jittered backoff and a capped `Retry-After`, log a warning per transient failure, and raise when
spent. A check refuses a bare client anywhere else.
*Why:* hand-written retry loops each answer "spent" differently. *Skip when* the call is a stream mid-turn, a
single-use code, or a non-idempotent request without an idempotency key.

**A session that resumes.** Number every message per direction, acknowledge the highest one applied in order, and
buffer the unacknowledged ones for a bounded time. A reconnect sends the session key and the last acknowledged
number; the far end replays what was missed or says loudly that the state is gone. The receiver drops duplicates.
*Why:* a two-second drop otherwise resets the session and reruns a call in flight. *Skip when* the far end holds no
state worth keeping. A resume is not a retry.

**Idempotency key with a body fingerprint.** The client mints one key per logical operation and repeats it on every
attempt. The server stores the first result with a fingerprint of the body: the same key and body return the stored
result; the same key with another body is a 409.
*Why:* a lost response plus a retry would otherwise do it twice. *Skip when* the request is idempotent by
construction (a GET, a PUT by content hash, setting a named state).

**The same ask again is the same run.** A call that starts work over a transport that may drop its answer, from a
caller that cannot send a key, is safe to repeat: the same caller asking the same thing within a short window of a
run still starting or live gets that run back, said as already going.

**One poller.** Poll through one helper: the check returns a value to end or nothing for "not yet", the wait doubles
up to a maximum, a transient failure backs off, and giving up raises. *Skip when* a stream already carries the
update.

**One protocol door.** Put a component that may move to its own service behind one protocol in the caller's
package. Only the composition root builds the implementation; an import rule keeps everyone else, tests included, off
the component. *Why:* when it moves, the protocol gets a network client and no caller changes.

**A one-shot container as a work step.** Run untrusted or heavy work as a one-shot container behind a one-method
runner protocol; tests inject a fake that writes the files a real container would leave. The container sees one
mounted folder and an environment built for the run. *Skip when* the work can run in-process and be trusted.

**A worker streams progress to a file.** A long-running worker appends one JSON object per line to a file both
sides share, stops at a named maximum, and spends its last line saying the file is full. The reader refuses a
malformed whole line and leaves an unfinished last line for the next read.

**Stop a job through its own cancel scope,** and wait for it, so a write already running in a worker thread cannot
land after the stop returns.

## Failures and what leaves a process

**Allow-list what leaves the process.** For error trackers, log shippers and crash dumps, name what may ship and
drop the rest. Never scrub by a deny-list of secret-looking names. *Why:* a deny-list leaks every new secret once.

**Platform failures report; workflow failures surface.** A crashed container, a failed dependency or a bug goes to
the error tracker once, with its ids and failure class. A user's broken input or a job that hit its own limit goes
to the user's records and the UI, and nothing of its content reaches the tracker.

**A file read on every poll reports once,** and fails only its own row. A damaged stored item raises a typed error
naming it; a list draws that one row as unreadable; every reporter asks one report memory first, so the damage
reports once an hour, not on every poll.

**A runtime's boot failure is a sibling's report.** A part that fails while setting up its own reporter cannot
report. Its boot depends on nothing it can lack, and a boot failure crosses the one channel it always has to a
sibling with a working reporter.

## Code structure

**An agent's decision, applied within guardrails code enforces.** When a model decides something that changes
records, its answer is a proposal. Plain code after it refuses an answer off its shape as the agent's failure and
applies each decision only within rules written as code. Every proposal is recorded with its reason, accepted or
refused, and a refused one still counts toward the cap that ends the loop. *Skip when* the answer only informs a
person.

**Generic engine, pluggable strategies.** Keep the control flow in an engine that knows no case; put what varies in
small strategy objects. Adding a case adds a strategy, and the engine never changes. *Skip when* there is one case
and no second in sight.

**Checks over a shared context.** Build the expensive inputs once into a frozen context. Each rule is one module with
an id and a one-sentence rule, returning findings as records, never strings. Hold the checks in one ordered tuple,
and prove the set with a corpus whose cases name the findings they expect.

**Derive a per-instance credential** as `HMAC(secret, "<role>:<instance>")` instead of minting and storing it, when
nobody outside must rotate it.

**State that rides with the client, sealed.** When a call must stop to ask a person and resume on the next call, put
what it needs in the round trip, encrypted, bound to the caller and the call, and short-lived. Check it against the
world as it is now before acting on the answer.

**A number people reuse is a label, not an identity.** Mint an id once and store it beside the number; reads match
on the id and refuse a number that now names something else.

## Screens

**The server computes; the browser renders.** Send positions as numbers, URLs built by the one function that owns
the route, a state as both its word and its tone, and an action as `{label, url, body}` or nothing when the rules
refuse it. The browser posts it unchanged.

**Targeted updates.** In a view that redraws on a poll, build the nodes once and write only what changed, matching
list items by key, so focus, scroll and running motion survive.

**Pages mount in place.** One document; every page address maps to a module that mounts into the page region and
stops its live reads when it leaves. No reload between pages.

**Layers close from the top.** Every menu, dialog and panel is a layer on one stack; only the top layer answers
Escape and an outside click, and a menu closes only once its action happened.

**One frame and one tempo.** The tokens file holds the page frame and the motion timings; no stylesheet sets a page
width or a duration of its own.

**A strip of counts first, then each item's status.** A page that watches live work opens with a few counts in fixed
places, the reader's first questions in order. Each item below carries its state as a dot and a word, never colour
alone.

## Contracts and checks

**One contracts file, generated per language.** A value shared across components lives once, with a sentence saying
what it is; a generator writes each language's copy and a check refuses a stale one.

**Golden corpus across a language boundary.** When two languages validate the same input, check in one corpus of
valid and invalid cases with the expected problems, and test both sides against it.

**Registry coverage.** A check over registered instances misses unregistered ones. Add a detector that finds
instances mechanically, a registry of covered ones plus reasoned exclusions, and fail in both directions: an
instance no row claims, and a row that matches nothing.
