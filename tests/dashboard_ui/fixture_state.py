"""The stand-in factory behind the UI tests: fixture parts, plus just enough state for actions to show their effect."""

from __future__ import annotations

import copy
import html
import json
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

JSON = dict[str, Any]
TEST_CODE = "123456"
PAGE_SIZE = 25
WRONG_CODES_TO_LOCK = 5
SETTLED_S = 1.5
CHOOSING_S = 2.0
DONE_NEED_S = 1.6


def refusal(rule: str, reason: str, code: JSON | None = None) -> JSON:
    out: JSON = {"ok": False, "refused": rule, "reason": reason}
    if code is not None:
        out["code"] = code
    return out


def ok(message: str | None, refresh: list[str], op: str | None = None) -> JSON:
    out: JSON = {"ok": True, "refresh": refresh, "message": message}
    if op is not None:
        out["op"] = op
    return out


def page_of(rows: list[Any], page: int) -> tuple[list[Any], bool]:
    start = (page - 1) * PAGE_SIZE
    return rows[start : start + PAGE_SIZE], len(rows) > start + PAGE_SIZE


def ordinal(n: int) -> str:
    return str(n) + ("th" if 10 < n % 100 < 14 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th"))


def mark(text: str, q: str) -> str:
    out = html.escape(text, quote=False)
    if not q:
        return out
    low, needle, parts, i = out.lower(), html.escape(q, quote=False).lower(), [], 0
    while (j := low.find(needle, i)) >= 0:
        parts += [out[i:j], "<mark>", out[j : j + len(needle)], "</mark>"]
        i = j + len(needle)
    return "".join(parts) + out[i:]


class Op:
    def __init__(self, title: str, steps: list[str], on_done: Callable[[], str], fail_at: int | None) -> None:
        self.title, self.steps, self.on_done, self.fail_at = title, steps, on_done, fail_at
        self.t0 = time.monotonic()
        self.message: str | None = None


class State:
    def __init__(self, fixtures: Path) -> None:
        self.dir = fixtures
        self.reset()

    def load(self, name: str) -> JSON:
        data: JSON = json.loads((self.dir / name).read_text())
        return data

    def has(self, name: str) -> bool:
        return (self.dir / name).is_file()

    def reset(self) -> None:
        self.switches: JSON = {
            "down": False,
            "fail_path": None,
            "fail_part": [],
            "refuse": {},
            "slow_ms": 0,
            "slow_path": "/api/",
            "api": 1,
            "op_fail": False,
            "op_ms": 500,
            "scenario": None,
        }
        self.calls: list[JSON] = []
        self.seen: dict[str, tuple[int, JSON]] = {}
        self.ops: dict[str, Op] = {}
        self.wrong = 0
        self.shell = self.load("shell.json")["shell"]
        self.home = self.load("home.json")
        reserve = self.load("home-reserve.json")
        self.reserve: list[JSON] = reserve["cards"]
        self.outage_card: JSON = reserve["outage_card"]
        self.slots: list[JSON] = self.home["proposals"]["slots"]
        self.needs: list[JSON] = self.home["needs"]
        self.outage: JSON | None = None
        tasks = self.load("work-tasks.json")
        groups = {g["key"]: g for g in tasks["tasks"]["groups"]}
        self.groups = groups
        self.rows: dict[str, JSON] = {r["id"]: r for g in groups.values() for r in g["rows"]}
        self.queue: list[str] = [r["id"] for r in groups["queue"]["rows"]]
        self.approved: list[str] = []
        self.boosted: list[str] = []
        self.versions: dict[str, int] = {}
        self.requests = self.load("work-requests.json")["requests"]
        self.parked = self.load("work-parked.json")["parked"]
        self.agents = self.load("agents.json")
        self.stopped: set[str] = set()
        self.factory = "running"
        self.settings = self.load("settings.json")["settings"]
        self.guard = self.load("guard.json")
        self.push = self.load("push.json")["push"]
        self.envs = self.home["envs"]
        self.library = self.load("library-index.json")
        self.plan = self.load("spend.json")["plan"]
        self.banner: JSON | None = None
        self.home_over: JSON = {}

    # ---------- switches ----------
    def scenario(self, name: str) -> None:
        self.switches["scenario"] = name
        for endpoint in ("shell", "home", "agents", "spend"):
            file = f"{endpoint}@{name}.json"
            if not self.has(file):
                continue
            over = self.load(file)
            if endpoint == "shell":
                self.shell = over["shell"]
            elif endpoint == "home":
                self.outage = over.pop("outage", self.outage)
                self.envs = over.pop("envs", self.envs)
                self.home_over.update(over)
            elif endpoint == "agents":
                self.banner = over.get("banner")
                if "tree" in over:
                    self.agents["tree"] = over["tree"]
            elif endpoint == "spend":
                self.plan = over["plan"]
        self.factory = self.shell["factory"]

    def version(self, task: str) -> str:
        return f"v{self.versions.get(task, 1)}"

    def bump(self, task: str) -> None:
        self.versions[task] = self.versions.get(task, 1) + 1

    def stale(self, body: JSON, task: str) -> JSON | None:
        v = body.get("version")
        if v is not None and v != self.version(task):
            return refusal("stale", "This changed; here it is now.")
        return None

    def check_code(self, body: JSON, exempt: bool = False) -> JSON | None:
        lock = self.shell["code"]["locked_until"]
        if lock and not exempt:
            return refusal("locked", f"Coded actions are locked until {lock}.", {"locked_until": lock})
        if body.get("code") == TEST_CODE:
            self.wrong = 0
            return None
        self.wrong += 1
        if self.wrong >= WRONG_CODES_TO_LOCK:
            self.wrong = 0
            lock = (datetime.now() + timedelta(minutes=self.shell["code"]["lock_minutes"])).strftime("%H:%M")
            self.shell["code"]["locked_until"] = lock
            return refusal("locked", f"Coded actions are locked until {lock}.", {"locked_until": lock})
        return refusal("wrong-code", "That code is wrong.", {"tries_left": WRONG_CODES_TO_LOCK - self.wrong})

    # ---------- proposals: a settled card turns to "choosing", then the slot refills from the reserve ----------
    def proposals(self) -> JSON:
        now = time.monotonic()
        for s in self.slots:
            if s["kind"] == "settled" and now - s["_t"] > SETTLED_S:
                s.clear()
                s.update({"kind": "choosing", "_t": now})
        for s in [x for x in self.slots if x["kind"] == "choosing" and now - x["_t"] > CHOOSING_S]:
            self.slots.remove(s)
            if self.reserve:
                self.slots.append(self.reserve.pop(0))
        max_ = self.home["proposals"]["max"]
        cards = [s for s in self.slots if s["kind"] == "card"]
        for i, c in enumerate(cards):
            c.update(
                {"number": i + 1, "can_up": i > 0, "can_down": i < len(cards) - 1, "version": self.version(c["task"])}
            )
        nones = [
            {"kind": "none", "message": "" if i else "Nothing else worth proposing now."}
            for i in range(max_ - len(self.slots))
        ]
        return {"max": max_, "open": len(cards), "approved_waiting": len(self.approved), "slots": self.slots + nones}

    def card_slot(self, task: str) -> JSON | None:
        return next((s for s in self.slots if s["kind"] == "card" and s["task"] == task), None)

    def proposal_act(self, task: str, what: str, body: JSON) -> JSON:
        self.proposals()
        slot = self.card_slot(task)
        if slot is None:
            return refusal("not-proposed", "That task is no longer proposed; here is what is.")
        if bad := self.stale(body, task):
            return bad
        line, picture = slot["line"], slot["picture"]
        if what == "approve":
            slot.clear()
            slot.update(
                {
                    "kind": "settled",
                    "task": task,
                    "line": line,
                    "picture": picture,
                    "state": "approved",
                    "message": "Approved. It starts when a slot is free.",
                    "_t": time.monotonic(),
                }
            )
            self.approved.append(task)
            return ok("Approved. It starts when a slot is free.", ["home", "work", "shell"])
        if what == "later":
            slot.clear()
            slot.update(
                {
                    "kind": "settled",
                    "task": task,
                    "line": line,
                    "picture": picture,
                    "state": "later",
                    "message": "Back in the queue until something changes.",
                    "_t": time.monotonic(),
                }
            )
            self.queue.append(task)
            return ok("Not now. It goes back to the queue.", ["home", "work"])
        if what == "drop":
            slot.clear()
            slot.update({"kind": "choosing", "_t": time.monotonic()})
            return ok("Dropped. Its requests are answered.", ["home", "work"])
        if what == "run":
            run = str(body.get("run"))
            est = {"1": 6, "2": 14, "3": 28, "4": 34}.get(run)
            if est is None:
                return refusal("run", "That run is not one of the choices.")
            label = next(x[1] for x in slot["run"]["options"] if x[0] == run)
            slot.update({"run": {**slot["run"], "value": run}, "estimate": f"≈ ${est}", "run_changed": True})
            self.bump(task)
            return ok(f"The run is now {label}", ["home"])
        if what == "move":
            order = [s["task"] for s in self.slots if s["kind"] == "card"]
            if body.get("seen_order") != order:
                return refusal("stale", "This changed; here it is now.")
            i, j = order.index(task), order.index(task) + int(body.get("dir", 0))
            if not 0 <= j < len(order):
                return refusal("move", "It is already at the end.")
            a, b = self.slots.index(self.card_slot(order[i]) or {}), self.slots.index(self.card_slot(order[j]) or {})
            self.slots[a], self.slots[b] = self.slots[b], self.slots[a]
            return ok(f"Moved to number {j + 1}", ["home", "work"])
        return refusal("unknown", "That action is not known.")

    def stop_outage_fix(self) -> JSON:
        if self.outage is None or self.outage["stage"] != "fixing":
            return refusal("no-fix", "No fix is running.")
        self.outage = {
            **self.outage,
            "stage": "stopped",
            "session": None,
            "actions": [],
            "message": "You stopped the fix. It waits for your approval at the top of <b>Proposed next</b>.",
        }
        cards = [s for s in self.slots if s["kind"] == "card"]
        if len(cards) >= self.home["proposals"]["max"]:
            self.slots.remove(cards[-1])
        self.slots.insert(0, copy.deepcopy(self.outage_card))
        return ok("Fix stopped. It waits for your approval.", ["home", "work"])

    # ---------- needs ----------
    def needs_list(self) -> list[JSON]:
        now = time.monotonic()
        self.needs = [n for n in self.needs if not (n.get("done") and now - n["_t"] > DONE_NEED_S)]
        return self.needs

    def need_act(self, nid: str, what: str, body: JSON) -> JSON:
        n = next((x for x in self.needs_list() if x["id"] == nid), None)
        if n is None or n.get("done"):
            return refusal("gone", "That decision was already made.")
        if body.get("version") != n["version"]:
            return refusal("stale", "This changed; here it is now.")
        if what not in n["actions"] or what == "artifact":
            return refusal("unknown", "That action does not fit this card.")
        if what == "answer" and not str(body.get("text") or "").strip():
            return refusal("empty", "Write an answer first.")
        words = {
            "go": "Approved. The build starts next.",
            "feedback": "The designer is reading your comments.",
            "extend": "Two more rounds granted.",
            "designer": "Sent to a designer at rung 3.",
            "park": "Parked until the goal changes.",
            "answer": "Your answer is on its way to the session.",
        }
        n.update({"done": words[what], "_t": time.monotonic()})
        return ok(words[what], ["home", "shell"])

    # ---------- work ----------
    def task_groups(self) -> list[JSON]:
        out = [copy.deepcopy(self.groups["progress"])]
        if self.approved:
            rows = []
            for i, t in enumerate(self.approved):
                r = {
                    **self.rows[t],
                    "rank": i + 1,
                    "rank_mark": None,
                    "action": "order",
                    "can_up": i > 0,
                    "can_down": i < len(self.approved) - 1,
                    "status": {"tone": "good", "word": f"Approved, {ordinal(i + 1)} in line"},
                }
                rows.append(r)
            out.append(
                {
                    "key": "approved",
                    "title": "Approved, starting in this order",
                    "hint": "Each starts when a slot is free. Move one up to start it sooner.",
                    "empty": "Nothing approved is waiting.",
                    "rows": rows,
                }
            )
        cards = [s["task"] for s in self.proposals()["slots"] if s["kind"] == "card" and s["task"] in self.rows]
        prop = copy.deepcopy(self.groups["proposed"])
        prop["rows"] = [
            {
                **self.rows[t],
                "rank": i + 1,
                "rank_mark": None,
                "action": "approve",
                "boosted": False,
                "status": {"tone": "warn", "word": "Waiting for you"},
            }
            for i, t in enumerate(cards)
        ]
        out.append(prop)
        waiting = [t for t in self.queue if t not in cards and t not in self.approved]
        queue = [t for t in self.boosted[::-1] if t in waiting] + [t for t in waiting if t not in self.boosted]
        q = copy.deepcopy(self.groups["queue"])
        q["rows"] = [
            {
                **self.rows[t],
                "rank": i + 1,
                "rank_mark": None,
                "boosted": t in self.boosted,
                "status": {"tone": "ring", "word": "Queued"},
                "action": None if self.rows[t]["rung"] == 0 else ("unboost" if t in self.boosted else "boost"),
            }
            for i, t in enumerate(queue)
        ]
        out.append(q)
        out.append(copy.deepcopy(self.groups["staging"]))
        return out

    def work(self, tab: str, inbox: str, outcome: str, page: int) -> JSON:
        base = self.load(f"work-{tab}.json")
        tabs = base["tabs"]
        if tab == "tasks":
            base["tasks"] = {"groups": self.task_groups()}
        elif tab == "requests":
            rows = [
                r
                for r in self.requests["rows"]
                if (r.get("broken") and inbox == "all" and outcome == "all")
                or (not r.get("broken") and inbox in ("all", r["inbox"]) and outcome in ("all", r["_outcome"]))
            ]
            shown, more = page_of(rows, page)
            base["requests"] = {**self.requests, "rows": shown, "more": more}
        elif tab == "parked":
            base["parked"] = self.parked
        base["tabs"] = [
            [
                k,
                label,
                sum(len(g["rows"]) for g in self.task_groups())
                if k == "tasks"
                else len(self.parked["rows"])
                if k == "parked"
                else n,
            ]
            for k, label, n in tabs
        ]
        return base

    def approved_move(self, task: str, body: JSON) -> JSON:
        if body.get("seen_order") != self.approved:
            return refusal("stale", "This changed; here it is now.")
        i = self.approved.index(task)
        j = i + int(body.get("dir", 0))
        if not 0 <= j < len(self.approved):
            return refusal("move", "It is already at the end.")
        self.approved[i], self.approved[j] = self.approved[j], self.approved[i]
        return ok(None, ["work", "home"])

    def boost(self, task: str, on: bool) -> JSON:
        if task not in self.queue:
            return refusal("not-queued", "Only a queued task can be boosted.")
        if on:
            self.boosted = [t for t in self.boosted if t != task] + [task]
            return ok("Boosted: it is the next proposal", ["work", "shell"])
        self.boosted = [t for t in self.boosted if t != task]
        return ok(f"Back in its place: {self.rows[task]['title']}", ["work"])

    def boost_request(self, rid: str) -> JSON:
        r = next((x for x in self.requests["rows"] if x["id"] == rid and not x.get("broken")), None)
        if r is None or not r["can_boost"]:
            return refusal("not-waiting", "Only a waiting request can be boosted.")
        r.update({"outcome": {"text": "t-0032", "tone": "accent"}, "can_boost": False, "_outcome": "task"})
        return ok("Triaged now: it becomes a boosted task", ["work", "shell"])

    def unpark(self, tid: str) -> JSON:
        row = next((r for r in self.parked["rows"] if r["id"] == tid), None)
        if row is None:
            return refusal("not-parked", "That task is not parked.")
        self.parked["rows"].remove(row)
        return ok(f"Back in the queue: {row['title']}", ["work", "shell"])

    def task(self, tid: str) -> JSON:
        if not self.has(f"task/{tid}.json"):
            return {"task": {"$error": "This task is gone."}}
        t = self.load(f"task/{tid}.json")
        t["task"]["version"] = self.version(tid)
        if tid in self.boosted:
            t["task"]["actions"] = [a if a != "boost" else "unboost" for a in t["task"]["actions"]]
        return t
