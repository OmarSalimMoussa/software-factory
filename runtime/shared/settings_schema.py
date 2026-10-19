"""Every setting the factory has: what it may hold, and when a change to it takes effect. The settings object
checks the repo's defaults and the owner's overrides against this table.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Spec:
    """What one setting may hold, and when a change to it takes effect."""

    kind: str  # int, float, str, bool, size, list, model, prices, weights
    low: float | None = None  # for a list, the fewest items it may hold
    high: float | None = None  # for a list, the most
    required: bool = False
    when: str = "At once"
    item: str = "str"  # for a list, each item's kind: str (never blank), int (above zero) or id (a chat's id)


AT_ONCE, NEXT_START, MINUTE, ORCH = "At once", "Next start", "Within a minute", "Next orchestrator start"


# Wired by the install into what nothing else rewrites (the firewall, the edge's site, the check-in periods):
# changed only with the install's --set, so a save never leaves them out of step.
AT_INSTALL = "Set at install"


_ROLES = (
    "orchestrator designer implementer_large implementer_small intake triage critic qa_backend qa_security qa_ux "
    "qa_visual qa_ui_function qa_regression qa_prod"
).split()


SCHEMA: dict[str, Spec] = {
    "repo.path": Spec("str", required=True, when=ORCH),
    "repo.default_branch": Spec("str", required=True, when=ORCH),
    "repo.github": Spec("str", required=True, when=ORCH),
    # contract: the product's extra paths only a person changes, beyond the factory's own protected set.
    "repo.protected_paths": Spec("list", required=True),
    "models.allowed": Spec("list", 1, required=True, when=NEXT_START),
    "models.efforts": Spec("list", 1, required=True, when=NEXT_START),
    **{f"models.{role}": Spec("model", when=NEXT_START) for role in _ROLES},
    "caps.implementers": Spec("int", 1, 6),
    "caps.designers": Spec("int", 1, 4),
    "caps.subagents_at_once": Spec("int", 1, 8),
    "caps.browsers_at_once": Spec("int", 1, 16),
    "caps.desktops_at_once": Spec("int", 1, 16),
    "subagents.browser_personas": Spec("list"),
    "subagents.orchestrator": Spec("list"),
    "subagents.designer": Spec("list"),
    "subagents.implementer": Spec("list"),
    "subagents.unpermitted": Spec("list"),
    "rounds.qa": Spec("int", 1, 8),
    "rounds.critic": Spec("int", 1, 8),
    "rounds.check_runs": Spec("int", 1, 6),
    "rounds.extend_by": Spec("int", 1, 4),
    "rounds.bundle_merges": Spec("int", 1, 20),
    "rounds.revert_fails": Spec("int", 1, 5),
    "qa.seconds_per_item": Spec("int", 30, 600),
    "qa.setup_seconds": Spec("int", 0, 600),
    "qa.max_items": Spec("int", 1, 20),
    "qa.prod_every_hours": Spec("int", 1, 168),
    "qa.desktop_seconds": Spec("int", 60, 900),
    "qa.desktop_wait_minutes": Spec("int", 0, 6),
    "qa.keep_bundles": Spec("int", 1, 100),
    "qa.widths": Spec("list", 1, 6, item="int"),
    "qa.quiet_seconds": Spec("int", 30, 900),
    "qa.stale_seconds": Spec("int", 60, 3600),
    "qa.screenshots_for_owner_only": Spec("list"),
    "qa.max_steps": Spec("int", 1, 20),  # contract: the most numbered steps a standing scenario has per item
    "qa.viewport_height": Spec("int", 400, 2000),  # contract: the tester browser's height; widths come from qa.widths
    "qa.browser_permissions": Spec("list"),
    "qa.desktop_viewport": Spec("list", 2, 2, item="int"),  # contract: a desktop tester's browser, width then height
    "qa.wait_reload_seconds": Spec("int", 5, 600),
    "qa.key_delay_ms": Spec("int", 0, 1000),
    "qa.hold_max_seconds": Spec("int", 1, 300),
    "qa.click_hold_ms": Spec("int", 0, 2000),
    "qa.press_settle_ms": Spec("int", 0, 2000),
    "qa.place_turn_seconds": Spec("int", 10, 3600),
    "qa.wait_max_minutes": Spec("int", 1, 60),
    "qa.browser_answer_seconds": Spec("int", 10, 900),
    "qa.browser_answer_chars": Spec("int", 1000, 1_000_000),
    "replay.long_wait_seconds": Spec("float", 1, 60),
    "replay.wait_tail_seconds": Spec("float", 0.5, 30),
    "replay.navigation_tail_seconds": Spec("float", 0.2, 30),
    "replay.freeze_ms": Spec("int", 0, 2000),
    "replay.secret_seconds": Spec("int", 1, 300),
    "replay.recorder_before_seconds": Spec("float", 0, 5),
    "replay.recorder_after_seconds": Spec("float", 0, 5),
    "replay.desktop_view_share": Spec("float", 0.05, 1),
    "replay.read_part_share": Spec("float", 0.05, 1),
    "replay.card_ms": Spec("int", 0, 10000),
    "replay.fade_ms": Spec("int", 0, 10000),
    "replay.between_ms": Spec("int", 0, 10000),
    "replay.char_ms": Spec("int", 0, 10000),
    "replay.read_ms": Spec("int", 0, 10000),
    "replay.glide_ms": Spec("int", 0, 10000),
    "replay.before_ms": Spec("int", 0, 10000),
    "replay.after_ms": Spec("int", 0, 10000),
    "replay.miss_ms": Spec("int", 0, 10000),
    "replay.type_char_ms": Spec("int", 0, 10000),
    "replay.type_lead_ms": Spec("int", 0, 10000),
    "replay.type_rest_ms": Spec("int", 0, 10000),
    "replay.pill_ms": Spec("int", 0, 10000),
    "replay.shot_ms": Spec("int", 0, 10000),
    "intake.batch_size": Spec("int", 10, 500),
    "intake.batch_max_wait_hours": Spec("int", 1, 48),
    "intake.fast_lane_minutes": Spec("int", 5, 60),
    "intake.weights": Spec("weights"),
    "intake.minor_severity": Spec("float", 0, 1),
    "intake.requests_full": Spec("int", 2, 200),
    "intake.users_full": Spec("int", 2, 200),
    "intake.age_full_days": Spec("int", 1, 90),
    "intake.rescore_hours": Spec("int", 1, 48),
    "routing.small_max_slices": Spec("int", 1, 3),
    "routing.large_max_slices": Spec("int", 1, 6),
    "routing.max_proposals": Spec("int", 1, 5),
    "routing.estimate_min_runs": Spec("int", 1, 50),
    "routing.estimate_window": Spec("int", 1, 200),
    "routing.cause_tries": Spec("int", 1, 10),
    "slots.count": Spec("int", 1, 6, when="At the next slot claim"),
    "slots.port_base": Spec("int", 1024, 65000, when=NEXT_START),
    "slots.log_tail_lines": Spec("int", 0, 500),
    "slots.wake_note_minutes": Spec("int", 0, 120),
    "slots.api_timeout_seconds": Spec("int", 1, 300),  # contract: one slot API or health call
    "slots.api_max_kib": Spec("int", 16, 8192),  # contract: the largest slot API answer a session sees
    "slots.verb_minutes": Spec("int", 1, 240),  # contract: added by builder 3, one scripts/dev verb
    "slots.run_seconds": Spec("int", 10, 3600),
    "slots.tail_max_kib": Spec("int", 1, 1024),
    "slots.cpu_shares": Spec("int", 2, 1024),
    "slots.same_up_minutes": Spec("int", 0, 1440),
    "slots.owner_address": Spec("str"),
    "slots.zero_origin": Spec("str", when=AT_INSTALL),
    "slots.keys_from": Spec("str", when=AT_INSTALL),  # read by root (software-slot-keys.service)
    "slots.keys": Spec("list", when=AT_INSTALL),
    "budget.slice_memory_high": Spec("size", when=MINUTE),
    "budget.slice_memory_max": Spec("size", when=MINUTE),
    "budget.slice_cpu_weight": Spec("int", 1, 10000, when=MINUTE),
    "budget.cpu_target_percent": Spec("float", 10, 100, when=MINUTE),
    "budget.cpu_floor_percent": Spec("float", 1, 100, when=MINUTE),
    "budget.cpu_window_seconds": Spec("int", 30, 3600),
    "budget.walk_hold_percent": Spec("float", 10, 100),
    "budget.walk_hold_minutes": Spec("int", 1, 240),
    "budget.test_memory_ceiling_mib": Spec("int", 512, 65536, when="Next install"),
    "budget.test_reserve_mib": Spec("int", 0, 65536),
    "vitals.memory_amber_gib": Spec("float", 0.5, 256),
    "vitals.memory_red_gib": Spec("float", 0.25, 256),
    "vitals.cpu_red_percent": Spec("float", 10, 100),
    "vitals.battery_amber": Spec("int", 0, 100),
    "vitals.battery_red": Spec("int", 0, 100),
    "vitals.firewall_stale_seconds": Spec("int", 30, 3600),
    "vitals.disk_amber_gib": Spec("int", 1, 100000),
    "vitals.disk_red_gib": Spec("int", 1, 100000),
    "vitals.merge_wait_amber_hours": Spec("int", 1, 168),
    "watch.tick_seconds": Spec("int", 5, 120),
    "watch.probe_seconds": Spec("int", 15, 600, when=AT_INSTALL),
    "watch.outage_after": Spec("int", 2, 5),
    "watch.http_timeout_seconds": Spec("int", 1, 60),  # contract: added by 1B-2
    "watch.inbox_wait_seconds": Spec("int", 5, 300),  # contract: added by 1B-2
    "watch.push_wait_minutes": Spec("int", 5, 240),
    "watch.resume_tries": Spec("int", 1, 5),
    "watch.crash_window_minutes": Spec("int", 5, 1440),
    "watch.resume_backoff_seconds": Spec("int", 5, 600),
    "watch.daemon_settle_seconds": Spec("int", 0, 300),
    "watch.bench_grace_seconds": Spec("int", 10, 300),
    "watch.bench_min_run_minutes": Spec("int", 0, 240),
    "watch.account_check_seconds": Spec("int", 30, 900),
    "watch.container_deaths_minutes": Spec("int", 1, 60),
    "watch.failing_alarm_minutes": Spec("int", 5, 1440),
    "watch.done_minutes": Spec("int", 1, 60),
    "watch.reap_retry_minutes": Spec("int", 1, 1440),
    "watch.start_grace_seconds": Spec("int", 30, 600),  # contract: a start's process must appear within this
    "watch.respawn_poll_seconds": Spec("int", 1, 30),
    "watch.message_seconds": Spec("int", 3, 60),  # contract: how long the owner's message is held out to a session
    "watch.stop_seconds": Spec("int", 1, 30),
    "watch.loop_minutes": Spec("int", 5, 120, when=AT_INSTALL),
    "watch.orchestrator_silent_minutes": Spec("int", 10, 180),
    "watch.orchestrator_busy_silent_minutes": Spec("int", 10, 600),
    "watch.refusals_to_notify": Spec("int", 1, 50),
    "watch.refusal_window_minutes": Spec("int", 5, 1440),
    "watch.refusal_burst_seconds": Spec("int", 0, 300),
    "watch.socket_read_seconds": Spec("int", 1, 120),  # contract: added by 1B-3, a request line must arrive by then
    "queue.max_upload_kib": Spec("int", 16, 8192),
    "queue.max_brief_kib": Spec("int", 256, 12288),
    "checks.run_minutes": Spec("int", 1, 240),
    "checks.place_wait_minutes": Spec("int", 0, 240),
    "checks.test_seconds": Spec("int", 1, 600),
    "checks.test_workers": Spec("int", 1, 64),
    "checks.output_kib": Spec("int", 4, 1024),
    "checks.writable_caches": Spec("list"),  # folders in the agent's home a check may write
    "errors.poll_minutes": Spec("int", 1, 120),  # contract: added by FB6, prod's GlitchTip issues become requests
    "errors.max_pages": Spec("int", 1, 50),
    "errors.severe_events": Spec("int", 1, 1_000_000),
    "reports.poll_minutes": Spec("int", 1, 1440),
    "reports.fetch_seconds": Spec("int", 1, 120),
    "reports.max_mib": Spec("int", 1, 64),
    "telemetry.max_items": Spec("int", 1, 200),  # contract: added by FB6, `factory errors` and `factory logs`
    "telemetry.max_kib": Spec("int", 4, 1024),
    "telemetry.timeout_seconds": Spec("int", 1, 120),
    "chat.channels": Spec("list", item="id"),
    "chat.senders": Spec("list", item="id"),
    "chat.tag": Spec("str", required=True),
    "chat.poll_minutes": Spec("int", 1, 1440),
    "chat.poll_pages": Spec("int", 1, 50),
    "chat.page_messages": Spec("int", 1, 100),  # Discord answers at most 100 messages a call
    "chat.read_pages": Spec("int", 1, 100),
    "chat.reply_chars": Spec("int", 20, 1500),
    "chat.max_kib": Spec("int", 4, 1024),
    "chat.timeout_seconds": Spec("int", 1, 120),
    "reflect.fold_hours": Spec("int", 1, 168),
    "reflect.max_chars": Spec("int", 80, 4000),  # contract: added by 1B-3
    "reflect.chart_days": Spec("int", 7, 90),
    "cost.daily_budget_usd": Spec("float", 1, 100000),
    "cost.plan_stale_minutes": Spec("int", 5, 240),
    "cost.plan_amber_percent": Spec("int", 1, 100),
    "cost.plan_red_percent": Spec("int", 1, 100),
    "prices": Spec("prices"),
    "dashboard.port": Spec("int", 1024, 65535, when=AT_INSTALL),
    "dashboard.refresh_seconds": Spec("int", 5, 120),
    "dashboard.stale_seconds": Spec("int", 15, 600),
    "dashboard.page_margin_seconds": Spec("int", 1, 60),
    "dashboard.wrong_codes_to_lock": Spec("int", 1, 20),
    "dashboard.lock_minutes": Spec("int", 5, 1440),
    "dashboard.device_days": Spec("int", 1, 365),
    "dashboard.hosts": Spec("list", 1, when=AT_INSTALL),
    "dashboard.page_size": Spec("int", 10, 500),
    "dashboard.done_commits": Spec("int", 10, 1000),
    "dashboard.history_page_size": Spec("int", 5, 100),
    "dashboard.probe_strip": Spec("int", 3, 60),
    "dashboard.working_now_max": Spec("int", 1, 24),
    "dashboard.max_threads": Spec("int", 2, 64, when="Next warden start"),
    "dashboard.retired_linger_minutes": Spec("int", 1, 30),
    "dashboard.idempotency_hours": Spec("int", 1, 168),  # contract: added by 1B-3
    "dashboard.warden_seconds": Spec("int", 1, 60),  # contract: added by 2A, its longest wait on the warden
    "dashboard.settled_seconds": Spec("int", 1, 30),  # contract: added by 2A, an acted-on card says so this long
    "dashboard.message_max_chars": Spec("int", 100, 20000),  # contract: the longest message the owner sends
    "dashboard.thread_messages": Spec("int", 1, 50),  # contract: the owner's messages a session's thread shows
    "dashboard.thread_tail_kib": Spec("int", 64, 65536),  # contract: how much of a transcript the thread reads
    "gc.every_hours": Spec("int", 1, 168),
    "gc.images_keep_days": Spec("int", 1, 90),
    "gc.build_cache_keep_hours": Spec("int", 1, 2160),
    "gc.logs_keep_days": Spec("int", 1, 365),
    "gc.docker_budget_gib": Spec("int", 5, 2000),
    # The factory's own repo on GitHub (owner/name), which it never works on; the installer sets it.
    "factory.github": Spec("str", when="Next install"),
    "factory.command_seconds": Spec("int", 10, 900),
    "factory.warden_threads": Spec("int", 2, 64, when="Next warden start"),  # contract: added by 1B-3
    # contract: added by builder 3, read by the installer only.
    "install.hook_timeout_seconds": Spec("int", 2, 60, when="Next install"),
    "install.mcp_start_seconds": Spec("int", 10, 600, when="Next install"),
    "install.firewall_refresh_seconds": Spec("int", 10, 600, when="Next install"),
    "install.healthchecks_grace_minutes": Spec("int", 1, 120, when="Next install"),
    "install.walk_wait_minutes": Spec("int", 0, 240),
    "install.upgrade_wait_minutes": Spec("int", 5, 240),
    "install.upgrade_minutes": Spec("int", 10, 600, when="Next install"),
    # contract: added by FB7. The programs the product's scripts need, copied from the owner's PATH for the agents and
    # the stack.
    "install.toolchain": Spec("list", when="Next install"),
}
