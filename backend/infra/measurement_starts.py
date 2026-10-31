"""Canonical Measurement live source first-row queries.

The v147 migration keeps its own frozen copies of the historical source queries.
"""

from __future__ import annotations

from collections.abc import Iterable

from core.correction_attribution import FIRST_ARRIVAL_SQL
from core.correction_learning import TRIAGE_EVENT
from core.measurement_token_contract import priceable_usage_predicate
from core.size_rating import SIZE_EVENT


def source_group(metric: str) -> str:
    """Return the metric group whose start and marks it shares."""
    if metric == "outcomes":
        return "outcomes"
    if metric == "first_try_acceptance":
        return "first_try"
    if metric in {"took_recommendation_pct", "answered_other_pct", "answered_neither_pct"}:
        return "decisions"
    if metric in {"fresh_tokens", "fresh_tokens_per_outcome"}:
        return "tokens"
    if metric in {"corrections_triaged_pct", "corrections_untriaged_idle_pct", "idle_untriaged_threads"}:
        return "correction_triage"
    if metric in {"active_hours_per_outcome", "outcomes_per_active_hour", "time_on_platform_h"}:
        return "presence"
    return metric


_FIRST_ROW_SQL = {
    "accepted_outcomes": """
        SELECT e.created_at FROM plan_node_events e INDEXED BY idx_plan_node_events_created
        JOIN plan_nodes n ON n.id=e.node_id
        WHERE n.kind='task' AND e.new_state IN ('accepted','accepted_with_followup')
        ORDER BY e.created_at LIMIT 1
    """,
    "answered_decisions": """
        SELECT MIN(json_extract(ai.metadata,'$.plan_decision.answered_at'))
        FROM artifact_identities AS ai INDEXED BY idx_measurement_decisions_answered
        WHERE ai.integration='local' AND ai.type='plan-decision' AND json_valid(ai.metadata)
    """,
    "priceable_usage": f"""
        SELECT MIN(u.created_at) FROM llm_usage u WHERE {priceable_usage_predicate("u")}
    """,
    "presence": "SELECT MIN(v.entered_at) FROM presence_visits v",
    "accepted_size_ratings": f"""
        SELECT e.created_at FROM plan_node_events e INDEXED BY idx_plan_node_events_created
        JOIN plan_nodes n ON n.id=e.node_id
        WHERE n.kind='task' AND e.new_state IN ('accepted','accepted_with_followup')
          AND (n.size IS NOT NULL AND n.size != 'unknown' OR EXISTS (
            SELECT 1 FROM outcome_events f INDEXED BY idx_measurement_size_rater
            WHERE f.event_name='{SIZE_EVENT}' AND f.retracted_at IS NULL
              AND f.subject_key='plan_node:'||n.id
              AND json_extract(f.attributes,'$.rater') > ''
          ))
        ORDER BY e.created_at LIMIT 1
    """,
    "classified_corrections": FIRST_ARRIVAL_SQL,
    # Triage had no structured fact before it was first recorded, so coverage has no period earlier.
    "first_correction_triage": f"SELECT MIN(ts) FROM outcome_events WHERE event_name='{TRIAGE_EVENT}'",
    "limit_hits": """
        SELECT MIN(started_at) FROM active_turns WHERE status > ''
    """,
}


# A per-outcome ratio is measured from its first accepted outcome: a live install records its
# numerator from day one, so a period with no rejection or ask is a measured zero.
_LIVE_SOURCE = {
    "outcomes": "accepted_outcomes",
    "first_try": "accepted_outcomes",
    "rejections_per_outcome": "accepted_outcomes",
    "asks_per_outcome": "accepted_outcomes",
    # Corrections are measured from the earliest classified source (message, comment or decision answer): an earlier period had no classifier, so it is not a zero.
    "corrections_per_outcome": "classified_corrections",
    "asks_plus_corrections_per_outcome": "classified_corrections",
    "corrections_detected": "classified_corrections",
    "correction_triage": "first_correction_triage",
    "decisions": "answered_decisions",
    "tokens": "priceable_usage",
    "presence": "presence",
    "estimated_human_hours_per_active_hour": "accepted_size_ratings",
    "limit_hits": "limit_hits",
}


def source_first_row_sql(source: str) -> str:
    """Return the only SQL definition of one source's first-row UTC instant."""
    return _FIRST_ROW_SQL[source]


def has_live_start(group: str) -> bool:
    """Say whether a metric group has a live value source."""
    return group in _LIVE_SOURCE


def live_starts_query(groups: Iterable[str]) -> str:
    """Read each requested metric group's live value-source start."""
    requested = tuple(dict.fromkeys(groups))
    return "SELECT " + ", ".join(
        f"({source_first_row_sql(_LIVE_SOURCE[group])}) AS {group}" for group in requested
    )
