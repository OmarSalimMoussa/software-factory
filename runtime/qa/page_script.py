"""A tester's browser_evaluate script, run so that an error its own callbacks throw ends the call at once (wrapper.py).

Playwright's server waits for the promise a script returns, and sends nothing while it waits: a callback that throws
never settles that promise, so the call would wait out the wrapper's time. The wrapper sends the script inside a
harness that settles with the script's own answer, or fails with the first uncaught error or unhandled rejection whose
stack holds no web address: the page's own code always runs from one, the tester's script never does (recorder.js
tells them apart the same way). The page still sees and logs every error.

The harness holds no quote, backslash or line break, so Playwright's answer quotes it unchanged, and the wrapper cuts
it out of every answer: the tester reads back its own script.
"""

from __future__ import annotations

from typing import Any

EVALUATE = "browser_evaluate"
_BEFORE = (
    "(element) => new Promise((settle, fail) => { "
    "const own = (e) => e != null && !/https?:/.test(String(e.stack)); "
    "const done = () => { removeEventListener(`error`, thrown); removeEventListener(`unhandledrejection`, lost) }; "
    "const thrown = (event) => { if (own(event.error)) { done(); "
    "fail(new Error(`a callback of your script threw, so its promise would never settle: ${event.error}`)) } }; "
    "const lost = (event) => { if (own(event.reason)) { done(); "
    "fail(new Error(`a promise of your script was rejected and nothing caught it: ${event.reason}`)) } }; "
    "addEventListener(`error`, thrown); addEventListener(`unhandledrejection`, lost); "
    "Promise.resolve().then(() => { const run = ("
)
_AFTER = "); return typeof run === `function` ? run(element) : run }).then(settle, fail).finally(done) })"


def wrapped(params: dict[str, Any]) -> dict[str, Any]:
    """A browser_evaluate call with its script inside the harness; any other call as it is."""
    args = params.get("arguments")
    if params.get("name") != EVALUATE or not isinstance(args, dict) or not isinstance(args.get("function"), str):
        return params
    return {**params, "arguments": {**args, "function": f"{_BEFORE}{args['function']}{_AFTER}"}}


def unwrapped(line: str) -> str:
    return line.replace(_BEFORE, "").replace(_AFTER, "")
