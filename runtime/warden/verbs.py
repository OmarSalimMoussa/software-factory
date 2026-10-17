"""Every request the warden answers, the handler that answers it, and who may ask.

The socket server looks a verb up here, checks the caller's role and, for a subagent, its kind (a subagent acts in
its parent's role, but may call only the verbs that name its kind), and for a coded verb, the owner's 6-digit code.
Then it calls ``handler(ctx, args, data) -> dict``. A handler raises ``Refused`` to refuse. The guard reads the same
table, so a call this refuses never leaves the session.
Agent verbs arrive on warden.sock; owner verbs only on owner.sock, from the dashboard or the owner's own terminal.
"""

from __future__ import annotations

from dataclasses import dataclass

ORCH, IMPL, DESIGN, OWNER, DASH = "orchestrator", "implementer", "designer", "owner", "dashboard"
SESSIONS = frozenset({ORCH, IMPL, DESIGN})
ANY = frozenset({ORCH, IMPL, DESIGN, OWNER})
# Subagent kinds, by what they may do: the testers that record a verdict on an implementer's head, every tester, and
# the readers that place work in the code and the telemetry.
SECURITY = "qa-security"
VERDICTS = frozenset({"qa-backend", SECURITY, "qa-ui-function", "qa-ux", "qa-visual"})
TESTERS = VERDICTS | {"qa-regression", "qa-prod"}
INTAKE, TRIAGE = "intake", "triage"
READERS = TESTERS | {INTAKE, TRIAGE}
CRITICS = frozenset({"critic-architecture", "critic-product", "critic-reliability", "critic-security", "critic-ux"})


@dataclass(frozen=True, slots=True)
class Verb:
    handler: str  # "module:function"
    roles: frozenset[str]
    coded: bool = False
    owner_socket: bool = False
    kinds: frozenset[str] = frozenset()  # the subagent kinds that may call it; none but these


def _v(handler: str, *roles: str, kinds: frozenset[str] = frozenset()) -> Verb:
    return Verb(handler, frozenset(roles), kinds=kinds)


def _o(handler: str, coded: bool = False) -> Verb:
    return Verb(handler, frozenset({DASH, OWNER}), coded=coded, owner_socket=True)


VERBS: dict[str, Verb] = {
    # sessions and their lifecycle (1B-1)
    "start": _v("runtime.warden.sessions.lifecycle:start", ORCH, OWNER),
    "stop": _v("runtime.warden.sessions.lifecycle:stop", ORCH, OWNER),
    "resume": _v("runtime.warden.sessions.lifecycle:resume", ORCH, OWNER),
    "extend": _v("runtime.warden.sessions.limits:extend", ORCH, OWNER),
    "doing": _v("runtime.warden.sessions.lifecycle:set_doing", *SESSIONS),
    # contract: added by the coordinator; the guard's SubagentStart and SubagentStop hooks report a subagent
    "subagent.start": _v("runtime.warden.sessions.subagents:started", *SESSIONS),
    "subagent.end": _v("runtime.warden.sessions.subagents:ended", *SESSIONS),
    # the loop, probes, outage and pushes (1B-2)
    "tick": _v("runtime.warden.loop:tick", ORCH),
    "inbox": _v("runtime.warden.inbox:read", ORCH),
    "inbox.wait": _v("runtime.warden.inbox:wait", ORCH),
    "decide": _v("runtime.warden.owner.yolo:orchestrator_decide", ORCH),
    "check": _v("runtime.warden.checks:check", ORCH, IMPL),
    "promote": _v("runtime.warden.delivery.prod_push:promote_cli", OWNER),
    # reflections, status, settings (1B-3)
    "reflect": _v("runtime.warden.owner.reflections:append", *ANY, kinds=READERS | CRITICS),
    "status": _v("runtime.warden.status:status", *ANY, kinds=READERS),
    "settings.show": _v("runtime.warden.status:settings_show", *ANY, kinds=READERS),
    # contract: added by FB6, read-only telemetry; the guard lets only intake, triage and testers among subagents ask
    "errors": _v("runtime.warden.health.telemetry:errors", *SESSIONS, kinds=READERS),
    "traces": _v("runtime.warden.health.telemetry:traces", *SESSIONS, kinds=READERS),
    # the queue (1C)
    "request.new": _v("runtime.queue.verbs:request_new", *ANY),
    "triage.apply": _v("runtime.queue.verbs:triage_apply", ORCH),
    "triage.plan": _v("runtime.queue.verbs:triage_plan", ORCH, kinds=frozenset({TRIAGE})),
    "task.new": _v("runtime.queue.verbs:task_new", ORCH, kinds=frozenset({INTAKE})),
    "task.facts": _v("runtime.queue.verbs:task_facts", ORCH, kinds=frozenset({INTAKE})),
    "task.status": _v("runtime.queue.verbs:task_status", ORCH),
    "task.show": _v("runtime.queue.verbs:task_show", *SESSIONS, kinds=READERS | CRITICS),
    "queue.next": _v("runtime.queue.verbs:queue_next", ORCH, kinds=frozenset({INTAKE, TRIAGE})),
    "route": _v("runtime.queue.verbs:route", ORCH),
    "propose": _v("runtime.queue.verbs:propose", ORCH),
    "propose.none": _v("runtime.queue.verbs:propose_none", ORCH),
    "spec.save": _v("runtime.queue.verbs:spec_save", DESIGN),
    "brief.save": _v("runtime.warden.owner.briefs:save", DESIGN),
    "brief.comments": _v("runtime.warden.owner.briefs:comments", DESIGN),
    "brief.reply": _v("runtime.warden.owner.briefs:reply", DESIGN),
    "needs-owner": _v("runtime.queue.verbs:needs_owner", ORCH, DESIGN),
    "turn.next": _v("runtime.queue.turn:next_turn", ORCH),
    "queue.check": _v("runtime.queue.verbs:check_save", ORCH),  # contract: a saved check of the default branch
    # slots and QA (1C)
    "slot": _v("runtime.slots.verbs:slot", ORCH, IMPL, kinds=TESTERS),
    "qa.plan": _v("runtime.qa.verbs:qa_plan", IMPL),
    "qa-ready": _v("runtime.qa.verbs:qa_ready", IMPL),
    "qa-clean": _v("runtime.qa.verbs:qa_clean", IMPL),
    "ready-check": _v("runtime.qa.verbs:ready_check", IMPL, kinds=VERDICTS),
    # contract: a tester's verdict on the head, for qa-clean, counted from the findings it recorded
    "qa.result": _v("runtime.qa.verbs:qa_result", IMPL, kinds=VERDICTS),
    "qa.finding": _v("runtime.qa.verbs:qa_finding", IMPL, kinds=VERDICTS),
    # contract: where a tester's step starts and what it saw at its end, for the run's replay
    "qa.step": _v("runtime.qa.verbs:qa_step", IMPL, kinds=VERDICTS),
    "qa.step-result": _v("runtime.qa.verbs:qa_step_result", IMPL, kinds=VERDICTS),
    "qa.waive": _v("runtime.qa.verbs:qa_waive", ORCH),
    "cause": _v("runtime.queue.verbs:cause_try", IMPL),
    # the owner, from the dashboard or their own terminal
    "snapshot": _o("runtime.warden.snapshot:snapshot"),
    # contract: added by 2A. The dashboard cannot read the warden's state, so pictures and evidence come as bytes.
    "picture": _o("runtime.warden.snapshot:picture"),
    "evidence": _o("runtime.warden.snapshot:evidence"),
    "brief.page": _o("runtime.warden.owner.briefs:page"),
    "brief.comment": _o("runtime.warden.owner.briefs:comment"),
    "brief.resolve": _o("runtime.warden.owner.briefs:resolve"),
    "op.get": _o("runtime.warden.owner.ops:get"),
    "device.admit": _o("runtime.warden.owner.codes:admit_device", coded=True),
    "device.verify": _o(
        "runtime.warden.owner.codes:verify_device"
    ),  # contract: added by 1B-3, a device token ok/expired
    "proposal.approve": _o("runtime.queue.verbs:owner_approve"),
    "proposal.later": _o("runtime.queue.verbs:owner_later"),
    "proposal.drop": _o("runtime.queue.verbs:owner_drop"),
    "proposal.move": _o("runtime.queue.verbs:owner_move"),
    "proposal.run": _o("runtime.queue.verbs:owner_run"),
    "approved.move": _o("runtime.queue.verbs:owner_approved_move"),
    "task.boost": _o("runtime.queue.verbs:owner_boost"),
    "task.unboost": _o("runtime.queue.verbs:owner_unboost"),
    "request.boost": _o("runtime.queue.verbs:owner_request_boost"),
    "parked.unpark": _o("runtime.queue.verbs:owner_unpark"),
    "needs.go": _o("runtime.queue.verbs:owner_go"),
    "needs.feedback": _o("runtime.queue.verbs:owner_feedback"),
    "needs.extend": _o("runtime.queue.verbs:owner_extend"),
    "needs.designer": _o("runtime.queue.verbs:owner_to_designer"),
    "needs.park": _o("runtime.queue.verbs:owner_park"),
    "needs.answer": _o("runtime.queue.verbs:owner_answer"),
    "needs.fixed": _o("runtime.queue.verbs:owner_fixed"),
    "agent.message": _o("runtime.warden.owner.messages:send"),  # contract: the owner's message to a running session
    "agent.stop": _o("runtime.warden.sessions.lifecycle:owner_stop"),
    "orchestrator.start": _o("runtime.warden.sessions.lifecycle:owner_start_orchestrator"),
    "factory.pause": _o("runtime.warden.sessions.revive:owner_pause"),
    "factory.resume": _o("runtime.warden.sessions.revive:owner_resume"),
    "factory.upgrade": _o("runtime.warden.owner.factory_upgrade:start"),
    "outage.stop": _o("runtime.warden.delivery.outage:owner_stop_fix"),
    "settings.save": _o("runtime.warden.owner.settings_save:save"),
    "repo.change": _o("runtime.warden.owner.settings_save:change_repo"),
    "rule.switch": _o("runtime.warden.owner.settings_save:switch_rule"),
    "push.prod": _o("runtime.warden.delivery.prod_push:push_prod"),
    "push.back": _o("runtime.warden.delivery.prod_push:push_back"),
    "staging.push": _o("runtime.warden.delivery.github_push:owner_push"),
    "slot.open": _o("runtime.warden.owner.slot_sign_in:open_slot"),
    "yolo.switch": _o("runtime.warden.owner.yolo:owner_switch"),
    # contract: added by FB9, the agents' Claude accounts. No code: only the owner's own uid reaches owner.sock.
    "account.list": _o("runtime.warden.owner.accounts:list_accounts"),
    "account.add": _o("runtime.warden.owner.accounts:add"),
    "account.remove": _o("runtime.warden.owner.accounts:remove"),
    "account.use": _o("runtime.warden.owner.accounts:use"),
}
