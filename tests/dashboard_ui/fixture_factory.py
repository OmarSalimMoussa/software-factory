"""The rest of the stand-in factory: agents, the factory's switches, settings, guard rules, pushes and operations."""

from __future__ import annotations

import copy
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from fixture_search import flat, library, search
from fixture_state import JSON, Op, State, ok, page_of, refusal

ORCH = "orchestrator"


def show(row: JSON, v: Any) -> str:
    if row["control"] == "model":
        return f"{v['model']} · {v['effort']}"
    if row["control"] == "list":
        return ", ".join(v)
    return str(v)


class Factory(State):
    def reset(self) -> None:
        super().reset()
        self.triage_at = (datetime.now(UTC) + timedelta(minutes=118)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ---------- reads ----------
    def shell_view(self) -> JSON:
        return {"shell": {**self.shell, "factory": self.factory}}

    def home_view(self) -> JSON:
        h = copy.deepcopy(self.home)
        h["tiles"]["requests"]["triage_at"] = self.triage_at
        parts = {"envs": self.envs, "outage": self.outage, "proposals": self.proposals(), "needs": self.needs_list()}
        return {**parts, "tiles": h["tiles"], "goal": h["goal"], "working": h["working"], **self.home_over}

    def stopped_ids(self) -> set[str]:
        out: set[str] = set()

        def walk(n: JSON, under: bool) -> None:
            if n.get("broken"):
                return
            hit = under or n["id"] in self.stopped
            if hit:
                out.add(n["id"])
            for k in n["kids"]:
                walk(k, hit)

        if self.agents["tree"] is not None:
            walk(self.agents["tree"], False)
        return out

    def agents_view(self, page: int) -> JSON:
        a = copy.deepcopy(self.agents)
        gone = self.stopped_ids()

        def mark(n: JSON) -> None:
            if n.get("broken"):
                return
            if n["id"] in gone and not n["retired"]:
                n.update(
                    {
                        "marker": "stop",
                        "state_word": "stopped",
                        "can_stop": False,
                        "retired": True,
                        "doing": "Stopped by you",
                    }
                )
            for k in n["kids"]:
                mark(k)

        if a["tree"] is not None:
            mark(a["tree"])
        rows, more = page_of(a["history"]["rows"], page)
        a["history"] = {"rows": rows, "more": more}
        a["banner"] = self.banner
        return a

    def agent(self, aid: str) -> JSON:
        if not self.has(f"agent/{aid}.json"):
            return {"agent": {"$error": "This agent is gone."}}
        a = self.load(f"agent/{aid}.json")
        a["agent"]["version"] = self.version("agent:" + aid)
        if aid in self.stopped_ids():
            a["agent"].update(
                {
                    "actions": [],
                    "life_note": {
                        "kind": "retired",
                        "text": "Retired. It can never be resumed. It moves to History in a few minutes.",
                    },
                }
            )
        return a

    def preview(self, aid: str) -> JSON:
        name = f"agent/{aid}-stop-preview.json"
        if not self.has(name):
            return {"preview": {"$error": "This agent is gone."}}
        pv = self.load(name)
        gone, node = self.stopped_ids(), next((n for n in flat(self.agents["tree"]) if n["id"] == aid), None)
        if node is not None and aid != ORCH:
            live = [n for n in flat(node) if not n["retired"] and n["id"] not in gone]
            pv["preview"]["rows"] = [
                {k: n[k] for k in ("family", "initials", "name", "role", "model", "cost")} for n in live
            ]
            pv["preview"]["button"] = f"Stop {len(live)} agents" if len(live) > 1 else "Stop it"
        return pv

    def history(self, hid: str) -> JSON:
        name = f"history/{hid}.json"
        return self.load(name) if self.has(name) else {"history": {"$error": "This record is gone."}}

    def doc(self, did: str) -> JSON:
        name = f"doc/{did}.json"
        return self.load(name) if self.has(name) else {"doc": {"$error": "This file is gone."}}

    def spend(self, rng: str, slice_: str, page: int) -> JSON:
        r, s = self.load(f"spend-range-{rng}.json"), self.load(f"spend-slice-{slice_}.json")["slice"]
        rows, more = page_of(s["rows"], page)
        return {"plan": self.plan, "hero": r["hero"], "chart": r["chart"], "slice": {**s, "rows": rows, "more": more}}

    def reflect(self, kind: str, page: int) -> JSON:
        d = self.load("reflect.json")["reflect"]
        rows, more = page_of([e for e in d["entries"] if kind in ("all", e["kind"])], page)
        return {"reflect": {**d, "entries": rows, "more": more}}

    def library_view(self, q: dict[str, str]) -> JSON:
        return {"library": library(self.library, q)}

    def search_view(self, q: str) -> JSON:
        rows = [r for g in self.settings["groups"] for r in g["rows"]]
        return {
            "search": search(
                q,
                self.library["docs"],
                self.shell["pages"],
                self.agents["tree"],
                self.agents["history"]["rows"],
                rows,
                self.settings["groups"],
            )
        }

    # ---------- the factory itself: stop, start, pause, resume ----------
    def stop(self, aid: str, body: JSON) -> JSON:
        if aid == ORCH and (bad := self.check_code(body, exempt=True)):
            return bad
        if body.get("version") is not None and body["version"] != self.version("agent:" + aid):
            return refusal("stale", "This changed; here it is now.")
        if aid in self.stopped_ids():
            return refusal("gone", "That agent has already stopped.")
        self.stopped.add(aid)
        if aid == ORCH:
            self.factory = "stopped"
            self.banner = self.load("agents@stopped.json")["banner"]
            return ok("The factory is stopped", ["agents", "home", "shell"])
        name = self.load(f"agent/{aid}.json")["agent"]["name"]
        return ok(f"{name} stopped", ["agents", "home", "shell"])

    def start_orchestrator(self) -> JSON:
        if self.factory != "stopped":
            return refusal("running", "The orchestrator is already running.")
        self.stopped.clear()
        self.factory, self.banner = "running", None
        return ok("The orchestrator is running", ["agents", "home", "shell"])

    def pause(self, body: JSON) -> JSON:
        if bad := self.check_code(body, exempt=True):
            return bad
        if self.factory != "running":
            return refusal("not-running", "The factory is not running, so there is nothing to pause.")

        def done() -> str:
            self.factory, self.banner = "paused", self.load("agents@paused.json")["banner"]
            return "The factory is paused. Nothing is lost."

        return self.start_op(
            "Pausing the factory",
            [
                "Telling every agent to pause on its next call",
                "Waiting for each to commit its work",
                "Stopping their processes and their stacks",
                "Every agent is benched with its context kept",
            ],
            done,
        )

    def resume(self, body: JSON) -> JSON:
        if bad := self.check_code(body):
            return bad
        if self.factory != "paused":
            return refusal("not-paused", "The factory is not paused.")

        def done() -> str:
            self.factory, self.banner = "running", None
            return "The factory is running again."

        return self.start_op(
            "Resuming the factory",
            ["Starting the orchestrator with its full context", "Bringing benched agents back in order"],
            done,
        )

    # ---------- settings, rules, the repo ----------
    def save_settings(self, body: JSON) -> JSON:
        if bad := self.check_code(body):
            return bad
        rows = {r["key"]: r for g in self.settings["groups"] for r in g["rows"] if not r.get("broken")}
        changes: dict[str, Any] = body.get("changes") or {}
        if not changes:
            return refusal("empty", "There is nothing to save.")
        for k, v in changes.items():
            r = rows.get(k)
            if r is None:
                return refusal("unknown", f"There is no setting named {k}.")
            if r["control"] == "readonly":
                return refusal("readonly", f"{r['label']} is set by the installer.")
            if r["control"] == "number" and not (isinstance(v, int) and r["low"] <= v <= r["high"]):
                return refusal("range", f"{r['label']} must be from {r['low']} to {r['high']}.")
        sha = "5d0c2aa"

        def done() -> str:
            for k, v in changes.items():
                r = rows[k]
                self.settings["audit"].insert(
                    0, {"what": f"{r['label']} {show(r, r['saved'])} → {show(r, v)}", "sha": sha, "ago": "just now"}
                )
                r.update({"saved": v, "value": v})
            return f"Saved as {sha}. Nothing restarted."

        n = len(changes)
        return self.start_op(
            "Saving settings",
            [
                "Writing settings.toml",
                f"Committing as you: {sha}, “Settings: {n} change{'' if n == 1 else 's'}”",
                "Telling the warden to reread its settings",
                "No agent was stopped",
            ],
            done,
        )

    def change_repo(self, body: JSON) -> JSON:
        if bad := self.check_code(body):
            return bad
        path = str(body.get("path") or "").strip()
        if not path:
            return refusal("path", "Give the path of the product's checkout.")
        if self.factory == "running":
            return refusal("running", "Stop the orchestrator first: the factory works on one repo at a time.")

        def done() -> str:
            self.settings["repo"]["path"] = path
            return "The repo is changed. Start the orchestrator when you are ready."

        return self.start_op("Changing the repo", ["Checking the repo's shape", "Pointing the factory at it"], done)

    def rule(self, rid: str, body: JSON) -> JSON:
        if bad := self.check_code(body):
            return bad
        r = next((x for x in self.guard["rules"] if x["id"] == rid), None)
        if r is None:
            return refusal("unknown", "There is no such rule.")
        r["on"] = bool(body.get("on"))
        return ok(f"{rid} is {'on' if r['on'] else 'off'}", ["guard"])

    # ---------- push to prod, and back ----------
    def push_prod(self, body: JSON) -> JSON:
        if bad := self.check_code(body):
            return bad
        p = self.push
        if p["blocked"]:
            return refusal("blocked", p["blocked"])
        if not p["tasks"]:
            return refusal("nothing", "Prod already runs what staging runs.")
        if body.get("expected_staging_sha") != p["staging"]["sha"]:
            return refusal("stale", "Staging changed since you looked; here it is now.")
        sha = p["staging"]["sha"]

        def done() -> str:
            self.redeploy(sha)
            p["tasks"] = []
            return f"Prod now runs {sha}"

        return self.start_op(
            "Pushing to prod",
            [
                "Starting the push workflow",
                f"Redeploying prod at {sha}",
                "Waiting for prod to answer on the new build",
                "Starting a qa-prod pass",
            ],
            done,
        )

    def push_back(self, body: JSON) -> JSON:
        if bad := self.check_code(body):
            return bad
        sha = self.push["previous_prod"]
        if not sha:
            return refusal("nothing", "Prod has no earlier build to go back to.")

        def done() -> str:
            self.redeploy(sha)
            return f"Prod is back on {sha}"

        return self.start_op(
            "Pushing prod back",
            ["Starting the push workflow", f"Redeploying prod at {sha}", "Waiting for prod to answer"],
            done,
        )

    def redeploy(self, sha: str) -> None:
        old = self.push["prod"]["sha"]
        self.push["previous_prod"] = old
        self.push["prod"] = {**self.push["prod"], "sha": sha, "deployed": "just now"}
        self.shell["pills"]["prod"] = {"state": "healthy", "sha": sha}
        self.envs = {
            **self.envs,
            "to_push": 0,
            "prod": {
                **self.envs["prod"],
                "state": "healthy",
                "state_word": "Healthy",
                "sha": sha,
                "deployed": "just now",
            },
        }
        self.outage = None

    # ---------- operations: each step takes op_ms; a failing op stops at its second step ----------
    def start_op(self, title: str, steps: list[str], done: Callable[[], str]) -> JSON:
        oid = f"op-{len(self.ops) + 1:04d}"
        self.ops[oid] = Op(title, steps, done, 1 if self.switches["op_fail"] else None)
        return ok(None, [], op=oid)

    def op(self, oid: str) -> JSON:
        o = self.ops.get(oid)
        if o is None:
            return {"op": {"$error": "That operation is not known."}}
        i = int((time.monotonic() - o.t0) * 1000 / self.switches["op_ms"])
        state, steps = "running", []
        for j, text in enumerate(o.steps):
            st = "done" if j < i else "running" if j == i else "pending"
            if o.fail_at is not None and i >= o.fail_at:
                st = "done" if j < o.fail_at else "failed" if j == o.fail_at else "pending"
                state = "failed"
            steps.append(
                {"text": text, "state": st, "detail": "The workflow answered 502." if st == "failed" else None}
            )
        if state == "running" and i >= len(o.steps):
            state = "done"
            if o.message is None:
                o.message = o.on_done()
        result = {"message": o.message, "refresh": ["all", "shell"]} if state == "done" else {}
        return {"op": {"id": oid, "title": o.title, "state": state, "steps": steps, "result": result}}
