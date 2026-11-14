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
            "agent_hours returns, per vendor account (per provider when a call has "
            "no account) and for all together, agent-hours, agents, calls and token "
            "sums. An agent-hour is one agent in one clock hour with at least one "
            "model call, so one call counts the whole hour. agent_day_levels groups "
            "agent-days by kind of agent, by this project, another project or none, "
            "and by hours active that day. Both cover this user's agents in and "
            "outside projects. Both leave out hours the hourly usage summary has "
            "not built and count them in hours_left_out; agent_day_levels leaves "
            "out any day with such an hour and counts it in days_left_out. "
            "stored_message_pressure returns the top "
            "ten stored-message sizes as a proxy for unfolded backlog. Its "
            "rank_limit field is 10; matching threads beyond it are omitted. "
            "Each pressure row labels whether a current installed checkpoint "
            "exists. It does not "
            "estimate the exact rendered provider tail. The backend includes "
            "only this thread's user and project (seat_usage, agent_hours and agent_day_levels: user), with no raw messages, error "
            "text, or SQL. Every metric except stored_message_pressure requires since "
            "and until as UTC ISO 8601 timestamps no more than seven days apart; "
            "agent_hours takes whole hours and agent_day_levels takes 00:00:00Z. "
            "For stored_message_pressure, omit both for the full current scope "
            "or supply both to rank threads whose updated_at is in that window. "
            "On a pressure timeout, supply or narrow its activity window; "
            "for other metrics, narrow the required window once. "
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
        from api.usage_rollup import definition

        try:
            result = await run_in_db(
                lambda: read_audit_metric(
                    thread_id,
                    None,
                    kwargs["metric"],
                    since=kwargs.get("since"),
                    until=kwargs.get("until"),
                    rollup_definition=definition(),
                )
            )
        except AuditReadError as exc:
            return json.dumps(
                {"error": exc.category, "next": exc.detail}, separators=(",", ":")
            )
        return json.dumps(result, separators=(",", ":"))
