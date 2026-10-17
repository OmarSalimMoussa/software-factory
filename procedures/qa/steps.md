# Marking your steps

The owner watches each run of a tester an implementer starts as a replay, one step at a time. The replay knows where
a step starts and how it ended only from your marks, so mark every step you walk. A regression tester marks none:
its report's tables are its record.

- **Name the item as your brief does:** a scenario by its file name without `.md`, a check by its short name. A check
  with no numbered steps is one step, step 1.
- **Setup is not a step.** Sign in, load seeds and reach the starting page first. A step's start is marked as its own
  walk begins, and its result only once that walk is done.
- **Before a step,** say in one sentence what you will do, then mark its start:
  `factory qa step --head <sha> --step <item>:<n>`. The replay opens the step with your sentence.
- **After it,** mark how it ended, with what you saw in one sentence:
  `factory qa step-result --head <sha> --step <item>:<n> --result pass|fail|blocked --got "<what you saw>"`.
- **A finding** from a step names it the same way: add `--step <item>:<n>` to `factory qa finding`.
- **A step you do not reach** gets no mark. The replay shows it as not run.

Never put a link, a password or a token in a mark: the owner reads every word of it.
