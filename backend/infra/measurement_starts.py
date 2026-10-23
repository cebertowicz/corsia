"""Canonical install-wide Measurement source-start queries.

The migration and request path use the same definitions: a retained start never
moves later just because its underlying raw row is later deleted.
"""

from __future__ import annotations

from collections.abc import Iterable

from core.measurement_token_contract import priceable_usage_predicate
from core.size_rating import SIZE_EVENT, SUBJECT_PREFIX
from infra.database import write_transaction


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
          UNION ALL SELECT MIN(released_at) FROM plan_node_attempts INDEXED BY idx_measurement_first_try_released_bad WHERE release_reason IN ('failed','requirements_changed')
          UNION ALL SELECT MIN(created_at) FROM plan_node_events INDEXED BY idx_measurement_first_try_failed WHERE new_state='failed'
          UNION ALL SELECT MIN(created_at) FROM plan_node_events INDEXED BY idx_measurement_first_try_corrections WHERE event='correction_created'
        )
    """,
    "rejections_per_outcome": """
        SELECT MIN(e.created_at) FROM plan_node_events e WHERE e.event='review_rejected'
    """,
    "asks_per_outcome": """
        SELECT MIN(json_extract(ai.metadata,'$.plan_decision.raised_at'))
        FROM artifact_identities AS ai INDEXED BY idx_measurement_decisions_raised
        WHERE ai.integration='local' AND ai.type='plan-decision' AND json_valid(ai.metadata)
    """,
    "asks_hold": "SELECT MIN(created_at) FROM plan_node_holds INDEXED BY idx_measurement_user_holds_start WHERE owner_kind='user'",
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


def _store_model_version(db, identity: str | None, started_at: str) -> None:
    """Retain an identity's first accepted-task settlement instant."""
    if not identity:
        return
    with write_transaction(db, scope="record_measurement_model_version"):
        db.execute(
            "INSERT INTO measurement_model_versions(identity,started_at) VALUES (?,?) "
            "ON CONFLICT(identity) DO UPDATE SET started_at=MIN(started_at, excluded.started_at)",
            (identity, started_at),
        )


def record_model_version(db, identity: str | None, subject_key: str | None) -> None:
    """Retain a rating's rater at its task's first accepted settlement, if any."""
    if not identity or not subject_key or not subject_key.startswith(SUBJECT_PREFIX):
        return
    node_id = subject_key.removeprefix(SUBJECT_PREFIX)
    row = db.execute(
        "SELECT MIN(created_at) FROM plan_node_events WHERE node_id=? "
        "AND new_state IN ('accepted','accepted_with_followup')",
        (node_id,),
    ).fetchone()
    if row and row[0]:
        _store_model_version(db, identity, str(row[0]))


def record_accepted_node_model_versions(db, node_id: str, started_at: str) -> None:
    """Retain ratings that predate their node's newly accepted settlement."""
    rows = db.execute(
        "SELECT DISTINCT json_extract(attributes,'$.rater') FROM outcome_events "
        "WHERE subject_key=? AND event_name=? AND retracted_at IS NULL "
        "AND json_extract(attributes,'$.rater') > ''",
        (f"{SUBJECT_PREFIX}{node_id}", SIZE_EVENT),
    )
    for (identity,) in rows:
        _store_model_version(db, str(identity), started_at)


def record_existing_model_versions(db) -> None:
    """Seed retained model boundaries once during the schema migration."""
    for identity, started_at in db.execute(MODEL_VERSION_ROWS_SQL):
        _store_model_version(db, str(identity), str(started_at))
