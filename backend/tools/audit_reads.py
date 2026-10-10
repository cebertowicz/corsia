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
            "stored_message_pressure returns the top "
            "ten stored-message sizes as a proxy for unfolded backlog. Its "
            "rank_limit field is 10; matching threads beyond it are omitted. "
            "Each pressure row labels whether a current installed checkpoint "
            "exists. It does not "
            "estimate the exact rendered provider tail. The backend includes "
            "only this thread's user and project, with no raw messages, error "
            "text, or SQL. checkpoint_health and lease_reviews require since "
            "and until as UTC ISO 8601 timestamps no more than seven days apart. "
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
                        "lease created_at, or thread updated_at by metric."
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
        try:
            result = await run_in_db(
                lambda: read_audit_metric(
                    thread_id,
                    None,
                    kwargs["metric"],
                    since=kwargs.get("since"),
                    until=kwargs.get("until"),
                )
            )
        except AuditReadError as exc:
            return json.dumps(
                {"error": exc.category, "next": exc.detail}, separators=(",", ":")
            )
        return json.dumps(result, separators=(",", ":"))
