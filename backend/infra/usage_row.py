"""Canonical ledger row insertion inside the usage owner's transaction."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from infra.usage_rollup_schema import mark_late_insert

PLAN_FIELDS = ("plan_id", "plan_node_id", "plan_attempt_id", "plan_commitment_id", "plan_usage_role")
MEASURED_FIELDS = ("prompt_tokens", "completion_tokens", "cache_creation_tokens", "cache_read_tokens", "total_tokens", "cost_usd")


def write_usage_row(
    db: Any, *, usage_id: str, thread_id: str, turn_id: str | None,
    model: str, measured: Mapping[str, Any], ttft_ms: int | None,
    binding: Mapping[str, Any], source: str, now_iso: str,
    context_manifest_id: str | None = None, vendor_account_id: str | None = None,
    reasoning_effort: str | None = None,
) -> None:
    """Insert a frozen measurement and attribution, then touch model recency.

    ``reasoning_effort`` is the effort the call ran with; None when the path
    cannot know it (never a guessed default).
    """
    db.execute(
        """INSERT INTO llm_usage (
               id,thread_id,turn_id,model,prompt_tokens,completion_tokens,
               cache_creation_tokens,cache_read_tokens,total_tokens,cost_usd,
               ttft_ms,plan_id,plan_node_id,plan_attempt_id,
               plan_commitment_id,plan_usage_role,source,created_at,
               context_manifest_id,vendor_account_id,reasoning_effort
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            usage_id, thread_id, turn_id, model,
            *(measured.get(field) for field in MEASURED_FIELDS), ttft_ms,
            *(binding[field] for field in PLAN_FIELDS),
            source, now_iso, context_manifest_id, vendor_account_id, reasoning_effort,
        ),
    )
    mark_late_insert(db, now_iso)
    db.execute(
        "UPDATE models SET last_used_at = ?, updated_at = ? WHERE id = ? OR litellm_model = ?",
        (now_iso, now_iso, model, model),
    )
