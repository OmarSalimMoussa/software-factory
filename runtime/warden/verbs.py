"""Every request the warden answers, the handler that answers it, and who may ask.

The socket server looks a verb up here, checks the caller's role and, for a coded verb, the owner's 6-digit code,
then calls ``handler(ctx, args, data) -> dict``. A handler raises ``Refused`` to refuse.
Agent verbs arrive on warden.sock; owner verbs only on owner.sock, from the dashboard or the owner's own terminal.
"""

from __future__ import annotations

from dataclasses import dataclass

ORCH, IMPL, DESIGN, OWNER, DASH = "orchestrator", "implementer", "designer", "owner", "dashboard"
SESSIONS = frozenset({ORCH, IMPL, DESIGN})
ANY = frozenset({ORCH, IMPL, DESIGN, OWNER})


@dataclass(frozen=True, slots=True)
class Verb:
    handler: str  # "module:function"
    roles: frozenset[str]
    coded: bool = False
    owner_socket: bool = False


def _v(handler: str, *roles: str, coded: bool = False) -> Verb:
    return Verb(handler, frozenset(roles))


def _o(handler: str, coded: bool = False) -> Verb:
    return Verb(handler, frozenset({DASH, OWNER}), coded=coded, owner_socket=True)


VERBS: dict[str, Verb] = {
    # sessions and their lifecycle (1B-1)
    "start": _v("runtime.warden.lifecycle:start", ORCH, OWNER),
    "stop": _v("runtime.warden.lifecycle:stop", ORCH, OWNER),
    "resume": _v("runtime.warden.lifecycle:resume", ORCH, OWNER),
    "extend": _v("runtime.warden.lifecycle:extend", ORCH, OWNER),
    "bench": _v("runtime.warden.lifecycle:bench", ORCH),
    "revive": _v("runtime.warden.lifecycle:revive", ORCH),
    "doing": _v("runtime.warden.lifecycle:set_doing", *SESSIONS),
    # the loop, probes, outage and pushes (1B-2)
    "tick": _v("runtime.warden.loop:tick", ORCH),
    "inbox": _v("runtime.warden.inbox:read", ORCH),
    "inbox.wait": _v("runtime.warden.inbox:wait", ORCH),
    "push": _v("runtime.warden.github_push:push_checked", ORCH),
    "promote": _v("runtime.warden.github_push:promote_cli", OWNER),
    # reflections, status, settings (1B-3)
    "reflect": _v("runtime.warden.reflections:append", *ANY),
    "status": _v("runtime.warden.status:status", *ANY),
    "settings.show": _v("runtime.warden.status:settings_show", *ANY),
    # the queue (1C)
    "request.new": _v("runtime.queue.verbs:request_new", *ANY),
    "triage.apply": _v("runtime.queue.verbs:triage_apply", ORCH),
    "task.new": _v("runtime.queue.verbs:task_new", ORCH),
    "task.facts": _v("runtime.queue.verbs:task_facts", ORCH),
    "task.status": _v("runtime.queue.verbs:task_status", ORCH),
    "queue.next": _v("runtime.queue.verbs:queue_next", ORCH),
    "route": _v("runtime.queue.verbs:route", ORCH),
    "propose": _v("runtime.queue.verbs:propose", ORCH),
    "propose.none": _v("runtime.queue.verbs:propose_none", ORCH),
    "spec.save": _v("runtime.queue.verbs:spec_save", DESIGN),
    "card.comments": _v("runtime.queue.verbs:card_comments", DESIGN),
    "needs-owner": _v("runtime.queue.verbs:needs_owner", ORCH, DESIGN),
    "merge.recorded": _v("runtime.queue.verbs:merge_recorded", ORCH),
    # slots and QA (1C)
    "slot": _v("runtime.slots.verbs:slot", ORCH, IMPL),
    "qa.plan": _v("runtime.qa.verbs:qa_plan", IMPL),
    "qa-ready": _v("runtime.qa.verbs:qa_ready", IMPL),
    "qa-clean": _v("runtime.qa.verbs:qa_clean", IMPL),
    "ready-check": _v("runtime.qa.verbs:ready_check", IMPL),
    # the owner, from the dashboard or their own terminal
    "snapshot": _o("runtime.warden.snapshot:snapshot"),
    "op.get": _o("runtime.warden.ops:get"),
    "device.admit": _o("runtime.warden.codes:admit_device", coded=True),
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
    "agent.stop": _o("runtime.warden.lifecycle:owner_stop"),  # coded for the orchestrator: checked in the handler
    "orchestrator.start": _o("runtime.warden.lifecycle:owner_start_orchestrator"),
    "factory.pause": _o("runtime.warden.lifecycle:owner_pause", coded=True),
    "factory.resume": _o("runtime.warden.lifecycle:owner_resume"),
    "outage.stop": _o("runtime.warden.outage:owner_stop_fix"),
    "settings.save": _o("runtime.warden.settings_save:save", coded=True),
    "repo.change": _o("runtime.warden.settings_save:change_repo", coded=True),
    "rule.switch": _o("runtime.warden.settings_save:switch_rule", coded=True),
    "push.prod": _o("runtime.warden.github_push:push_prod", coded=True),
    "push.back": _o("runtime.warden.github_push:push_back", coded=True),
}
