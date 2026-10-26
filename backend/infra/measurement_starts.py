"""Canonical Measurement live source first-row queries.

The v147 migration keeps its own frozen copies of the historical source queries.
"""

from __future__ import annotations

from collections.abc import Iterable

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
    "rejected_outcomes": """
        SELECT MIN(e.created_at) FROM plan_node_events e WHERE e.event='review_rejected'
    """,
    "released_bad_attempts": """
        SELECT MIN(released_at) FROM plan_node_attempts
        WHERE release_reason IN ('failed','requirements_changed')
    """,
    "failed_outcomes": """
        SELECT MIN(created_at) FROM plan_node_events WHERE new_state='failed'
    """,
    "outcome_corrections": """
        SELECT MIN(created_at) FROM plan_node_events WHERE event='correction_created'
    """,
    "raised_decisions": """
        SELECT MIN(json_extract(ai.metadata,'$.plan_decision.raised_at'))
        FROM artifact_identities AS ai INDEXED BY idx_measurement_decisions_raised
        WHERE ai.integration='local' AND ai.type='plan-decision' AND json_valid(ai.metadata)
    """,
    "user_holds": """
        SELECT MIN(created_at) FROM plan_node_holds WHERE owner_kind='user'
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
    "limit_hits": """
        SELECT MIN(started_at) FROM active_turns WHERE status > ''
    """,
}


_LIVE_SOURCE = {
    "outcomes": "accepted_outcomes",
    "first_try": "accepted_outcomes",
    "rejections_per_outcome": "rejected_outcomes",
    "asks_per_outcome": "raised_decisions",
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


MODEL_VERSION_ROWS_SQL = f"""
    WITH ratings AS (
        SELECT json_extract(f.attributes,'$.rater') AS identity, e.created_at AS started_at,
               f.id AS rating_id
        FROM outcome_events f INDEXED BY idx_measurement_size_rater
        CROSS JOIN plan_nodes n ON n.id=substr(f.subject_key,11)
        JOIN plan_node_events e ON e.node_id=n.id
          AND e.new_state IN ('accepted','accepted_with_followup')
        WHERE f.event_name='{SIZE_EVENT}' AND f.retracted_at IS NULL
          AND f.subject_key GLOB 'plan_node:*'
          AND json_extract(f.attributes,'$.rater') > ''
    ), ordered AS (
        SELECT identity, started_at,
               LAG(identity) OVER (ORDER BY started_at, rating_id) AS prior_identity
        FROM ratings
    )
    SELECT identity, started_at FROM ordered
    WHERE prior_identity IS NOT NULL AND identity != prior_identity
    ORDER BY started_at, identity
"""
