"""Canonical install-wide Measurement source-start queries.

The migration and request path use the same definitions: a retained start never
moves later just because its underlying raw row is later deleted.
"""

from __future__ import annotations

from collections.abc import Iterable

from core.measurement_token_contract import priceable_usage_predicate
from core.size_rating import SIZE_EVENT


def source_group(metric: str) -> str:
    """Return the instrument group which owns a metric's retained start."""
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


ASKS_PRIMARY_SQL = """
    SELECT MIN(json_extract(ai.metadata,'$.plan_decision.raised_at')) AS started_at
    FROM artifact_identities AS ai INDEXED BY idx_measurement_decisions_raised
    WHERE ai.integration='local' AND ai.type='plan-decision' AND json_valid(ai.metadata)
"""

ASKS_HOLD_SQL = """
    SELECT MIN(created_at) AS started_at FROM plan_node_holds
    WHERE owner_kind='user'
"""


SOURCE_START_SQL = {
    "outcomes": """
        SELECT MIN(e.created_at) FROM plan_node_events e JOIN plan_nodes n ON n.id=e.node_id
        WHERE n.kind='task' AND e.new_state IN ('accepted','accepted_with_followup')
    """,
    "first_try": """
        SELECT MAX(started_at) FROM (
          SELECT MIN(e.created_at) AS started_at FROM plan_node_events AS e INDEXED BY idx_plan_node_events_created JOIN plan_nodes n ON n.id=e.node_id
          WHERE n.kind='task' AND e.new_state IN ('accepted','accepted_with_followup')
          UNION ALL SELECT MIN(created_at) FROM plan_node_events INDEXED BY idx_measurement_rejections_time WHERE event='review_rejected'
          UNION ALL SELECT MIN(released_at) FROM plan_node_attempts WHERE release_reason IN ('failed','requirements_changed')
          UNION ALL SELECT MIN(created_at) FROM plan_node_events WHERE new_state='failed'
          UNION ALL SELECT MIN(created_at) FROM plan_node_events WHERE event='correction_created'
        )
    """,
    "rejections_per_outcome": """
        SELECT MIN(e.created_at) FROM plan_node_events e WHERE e.event='review_rejected'
    """,
    "asks_per_outcome": f"""
        SELECT MAX(started_at) FROM (
          {ASKS_PRIMARY_SQL}
          UNION ALL {ASKS_HOLD_SQL}
        )
    """,
    "decisions": """
        SELECT MIN(json_extract(ai.metadata,'$.plan_decision.answered_at')) FROM artifact_identities AS ai INDEXED BY idx_measurement_decisions_answered
        WHERE ai.integration='local' AND ai.type='plan-decision' AND json_valid(ai.metadata)
    """,
    "tokens": f"""
        SELECT MIN(u.created_at) FROM llm_usage u
        WHERE {priceable_usage_predicate("u")}
    """,
    "presence": "SELECT MIN(v.entered_at) FROM presence_visits v",
    "estimated_human_hours_per_active_hour": f"""
        SELECT MIN(started_at) FROM (
          SELECT MIN(e.created_at) AS started_at
          FROM plan_node_events e INDEXED BY idx_plan_node_events_created
          JOIN plan_nodes n ON n.id=e.node_id
          WHERE n.kind='task' AND n.size IS NOT NULL AND n.size != 'unknown'
            AND e.new_state IN ('accepted','accepted_with_followup')
          UNION ALL
          SELECT MIN(e.created_at)
          FROM plan_node_events e INDEXED BY idx_plan_node_events_created
          JOIN plan_nodes n ON n.id=e.node_id
          WHERE n.kind='task' AND e.new_state IN ('accepted','accepted_with_followup')
            AND EXISTS (
              SELECT 1 FROM outcome_events f INDEXED BY idx_measurement_size_rater
              WHERE f.event_name='{SIZE_EVENT}' AND f.retracted_at IS NULL
                AND f.subject_key='plan_node:'||n.id
                AND json_extract(f.attributes,'$.rater') > ''
            )
        )
    """,
    "limit_hits": """
        SELECT MIN(started_at) FROM active_turns
        WHERE status > ''
    """,
}


# Live reads follow the stable primary instrument.  Migration-only SOURCE_START_SQL
# records the latest first prerequisite for legacy history where the old collector
# did not make every component observable from the primary stream.
LIVE_START_SQL = {
    "outcomes": SOURCE_START_SQL["outcomes"],
    "first_try": SOURCE_START_SQL["outcomes"],
    "rejections_per_outcome": SOURCE_START_SQL["rejections_per_outcome"],
    "asks_per_outcome": ASKS_PRIMARY_SQL,
    "decisions": SOURCE_START_SQL["decisions"],
    "tokens": SOURCE_START_SQL["tokens"],
    "presence": SOURCE_START_SQL["presence"],
    "estimated_human_hours_per_active_hour": SOURCE_START_SQL["outcomes"],
    "limit_hits": SOURCE_START_SQL["limit_hits"],
}


def live_starts_query(groups: Iterable[str]) -> str:
    """Read the primary live instrument for each requested metric group once."""
    return "SELECT " + ", ".join(f"({LIVE_START_SQL[group]}) AS {group}" for group in groups)


def source_start_columns(groups: Iterable[str]) -> list[str]:
    return [f"({SOURCE_START_SQL[group]}) AS {group}" for group in groups]


def source_starts_query(groups: Iterable[str]) -> str:
    return "SELECT " + ", ".join(source_start_columns(groups))


MODEL_VERSION_ROWS_SQL = f"""
    SELECT json_extract(f.attributes,'$.rater') AS identity, MIN(e.created_at) AS started_at
    FROM outcome_events f INDEXED BY idx_measurement_size_rater
    CROSS JOIN plan_nodes n ON n.id=substr(f.subject_key,11)
    JOIN plan_node_events e ON e.node_id=n.id
      AND e.new_state IN ('accepted','accepted_with_followup')
    WHERE f.event_name='{SIZE_EVENT}' AND f.retracted_at IS NULL
      AND f.subject_key GLOB 'plan_node:*'
      AND json_extract(f.attributes,'$.rater') > ''
    GROUP BY identity
    ORDER BY started_at, identity
"""
