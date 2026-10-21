# From an entry to an installed fix

## 1. Find the cause, on the server

Read before you change anything. Everything here is read-only:

- the session's moves in the warden's `history.jsonl`, and the sessions in its public `sessions.json`;
- the agent's transcript, the guard's `refusals.jsonl`, and the testers' step marks in the `qa-steps` folder;
- the stack logs, in the `stack` folder of the warden's `evidence`, and the warden's journal
  (`journalctl -u software-warden`).

Count from the factory's own records. A transcript line carries each tool result twice, so a grep over transcripts
counts every refusal double. A reported figure that turns out wrong is corrected to the owner.

## 2. Choose where the fix belongs

- **The factory's code**, when the factory let it happen. Align agents in code, not in prompts: a guard rule, a
  warden check, a tool that waits for what it needs, a refusal that names the exact command to run instead. Replay
  the change on the real transcripts that showed the fault before you trust it.
- **The product**, when its code or one of its scenarios is wrong. Tell the orchestrator, with the evidence and what
  "done" looks like; ask for a QA scenario that would catch it again.
- **Nothing**, when the factory already does the right thing and an agent slipped once. Mark it ignored with why.

Before a guard rule refuses a verb, look at how the product's procedures and scenarios use that verb: a rule that
blocks a legitimate use blocks every walk that needs it.

## 3. Brief a subagent

Each fix goes to a subagent in its own worktree (`isolation: worktree`), in the background. Several run at once
when they touch different files; tell each which files the others hold. A brief names:

- the problem in the reporter's own words, with its time and the evidence you found;
- what to confirm before fixing, and that the root cause comes first (the `systematic-debugging` skill), then a
  failing test through the public seam (`test-driven-development`);
- the fix you prefer, a fallback if it proves unsafe, and what must stay true;
- the rules that bite: [CLAUDE.md](../../../CLAUDE.md), the size ceilings, the comment ratchet, every number a
  setting, and the files already at their ceiling;
- read-only access to the server, with the secret rules of [traps.md](traps.md);
- one commit per slice with a plain message and the attribution lines; no push, no merge, no install;
- the verify and self-check commands below, run in the background, and not to stop while one still runs;
- what to report: the cause with evidence, the change and why, the commits, the verify tails, and what it could not
  confirm.

## 4. Review what comes back

Read the diff yourself. Check that the cause has evidence, the fix sits at that cause, and the tests go through the
public seam. A change to the guard is security code: it must show that nothing the shell runs escapes the guard,
for example by running fuzzed calls in real bash with a stand-in program.

## 5. Combine and verify

- Cherry-pick the finished branches onto main in a scratch worktree. Resolve a conflict by keeping both sides,
  then read the result.
- Check the size ceilings of every changed file. Two branches that each fit can together push a file over.
- Run `bash scripts/verify.sh <worktree>`: lint, format, types, every test folder, the comment density, and the
  browser drives.
- Run the self-check on a committed clone. Name the tip with a branch first, so the clone can find it:

  ```sh
  git branch -f batch <tip>; git clone -q <repo> <dir>; git -C <dir> checkout -q <tip>
  ln -s <a checkout's node_modules> <dir>/node_modules
  cd <dir> && FACTORY_CHECK_BASE=<the installed commit> uv run -q --python 3.12 --extra dev \
    pytest -q -p no:cacheprovider -m self_check tests/self_check
  ```

- A ratchet can fail on the combination although each branch passed. The comment density is rounded down per
  thousand lines, so one line can tip it. Fold a test's comment into its assertion's message, or a docstring into one
  line, rather than pin a ceiling.

## 6. Install

- Fast-forward main to the verified tip and push it to GitHub.
- Run `python3 scripts/install.py <full sha>`. factory.upgrade holds new testers and waits for running ones. The
  script follows the upgrade until the commit is installed, then resumes the factory, unless the factory was
  paused before.
- Install a batch at a time: an install benches every agent while it runs.
- Do not install while the owner is inspecting a pause: they asked to see the factory as it stopped.

## 7. After the install

- The warden's journal lists what it brought over from the old state (`brought the state over: …`). Check that the
  sessions came back, each implementer with its slot, and that the vitals are green.
- Then mark the entries the install addressed, each with its commit.
- Watch the first few minutes: a new Self-reflect entry right after an install is most often about the change.
