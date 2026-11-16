"""Agent-facing fixed audit reads with identity supplied by ToolRegistry."""

from __future__ import annotations

import json
from typing import Any

from infra.audit_reads import AUDIT_METRICS, AuditReadError, read_audit_metric
from infra.db_async import run_in_db
from tools.base import Tool


class AuditReadTool(Tool):
    """Read one bounded metric for the current thread's user and project."""

    @property
    def name(self) -> str:
        return "audit_read"

    @property
    def description(self) -> str:
        return (
            "Read one current, bounded CORSIA audit metric without a host lease. "
            "checkpoint_health counts checkpoints and recovered failures in a "
            "UTC window. lease_reviews groups each lease's state at read time "
            "by safe category, selecting leases created in the UTC window; "
            "it does not count verdicts issued during that window. "
            "seat_usage returns, per vendor account, call count and token sums, "
            "plus one row per harness with a null account for unattributed "
            "claude-code/ and codex/ usage; it covers this user's usage across "
            "all projects. Its token sums cover only calls Measurement can price; "
            "excluded_calls counts the rest. "
            "agent_hours returns, per vendor account (per provider, the model name up "
            "to its first '/', when a call has no account) and for all together, "
            "agent-hours, agents, calls and token sums. Unlike seat_usage, it covers "
            "every model and its token sums include calls Measurement cannot price, "
            "so the two can differ for one window. "
            "An agent-hour is one agent in one clock hour with at least one "
            "model call, so one call counts the whole hour. agent_day_levels groups "
            "agent-days by kind of agent, by this project, another project or none, "
            "and by hours active that day. Both cover this user's agents in and "
            "outside projects. "
            "plan_activity counts this user's plan activity across projects: agent-days, "
            "tasks created, conductor adds, stale open tasks, owed verdicts and plan_update calls; its "
            "row carries the definition of each field. "
            "limit_stops returns, per subscription account on this install, each usage "
            "window (window_key and window_kind, such as five_hour or seven_day) with any "
            "reading recorded before the UTC window's end: stops (times it "
            "reached 100%), hours_at_limit (hours at 100%, until the next reading or its "
            "reset) and observations (readings inside the window; 0 means no new reading). "
            "Accounts and windows with no recorded readings are absent. "
            "set_aside counts events on this install that were set aside instead of "
            "waking an agent, one row per internal_event and reason with its total and "
            "its count per UTC day (days with none are absent). A kind named by an agent "
            "or person, such as a declared event type or a script's event, is counted "
            "under internal_event 'declared', never by its name. "
            "stored_message_pressure returns the top "
            "ten stored-message sizes as a proxy for unfolded backlog. Its "
            "rank_limit field is 10; matching threads beyond it are omitted. "
            "Each pressure row labels whether a current installed checkpoint "
            "exists. It does not "
            "estimate the exact rendered provider tail. The backend includes "
            "only this thread's user and project (seat_usage, plan_activity, agent_hours and agent_day_levels: user; limit_stops: the install's accounts; set_aside: the install), with no raw messages, error "
            "text, or SQL. Every metric except stored_message_pressure requires since "
            "and until as UTC ISO 8601 timestamps no more than seven days apart; "
            "agent_hours takes whole hours and agent_day_levels takes 00:00:00Z. "
            "For stored_message_pressure, omit both for the full current scope "
            "or supply both to rank threads whose updated_at is in that window. "
            "On a timeout: for stored_message_pressure, supply or narrow its activity "
            "window; for agent_hours and agent_day_levels, narrow the window or call "
            "again in a few minutes; for other metrics, narrow the window once. "
            "A scope denial is final for this identity."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "required": ["metric"],
            "additionalProperties": False,
            "properties": {
                "metric": {"type": "string", "enum": list(AUDIT_METRICS)},
                "since": {
                    "type": "string",
                    "description": (
                        "UTC ISO 8601 inclusive start: checkpoint started_at, "
                        "lease created_at, llm_usage created_at, or thread updated_at by metric."
                    ),
                },
                "until": {
                    "type": "string",
                    "description": "UTC ISO 8601 exclusive end on the same time field.",
                },
            },
        }

    async def execute(self, **kwargs: Any) -> str:
        thread_id = kwargs.pop("_thread_id", None)
        kwargs.pop("_thread_project_id", None)
        kwargs.pop("_host_access_turn_identity", None)
        # Imported here, as other tools import the api layer, so loading the tool does not load it.
        from api import usage_rollup
        from api.audit_agent_hours import read_agent_hours

        if kwargs.get("metric") in ("agent_hours", "agent_day_levels"):
            usage_rollup.schedule_refresh()

        try:
            result = await run_in_db(
                lambda: read_audit_metric(
                    thread_id,
                    None,
                    kwargs["metric"],
                    since=kwargs.get("since"),
                    until=kwargs.get("until"),
                    agent_hours_reader=read_agent_hours,
                )
            )
        except AuditReadError as exc:
            return json.dumps(
                {"error": exc.category, "next": exc.detail}, separators=(",", ":")
            )
        return json.dumps(result, separators=(",", ":"))
