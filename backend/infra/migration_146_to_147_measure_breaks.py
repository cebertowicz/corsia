"""Schema 146 -> 147: persist Measurement breaks that source rows cannot record.

The frozen v147 definitions let upgrades from 146 or earlier keep working when live
ones change; the runtime read path retains its single definition in measurement_starts.
"""

from __future__ import annotations

from infra.measurement_schema import MEASUREMENT_BREAKS_SCHEMA_SQL

_MIGRATION_FIRST_ROW_SQL = {
    "accepted_outcomes": """
        SELECT MIN(e.created_at) FROM plan_node_events e JOIN plan_nodes n ON n.id=e.node_id
        WHERE n.kind='task' AND e.new_state IN ('accepted','accepted_with_followup')
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
    "priceable_usage": (
        "\n        SELECT MIN(u.created_at) FROM llm_usage u WHERE "
        "typeof(u.prompt_tokens)='integer' AND u.prompt_tokens>=0 AND "
        "typeof(u.completion_tokens)='integer' AND u.completion_tokens>=0 AND "
        "typeof(u.cache_creation_tokens)='integer' AND u.cache_creation_tokens>=0 AND "
        "typeof(u.cache_read_tokens)='integer' AND u.cache_read_tokens>=0 AND "
        "(u.total_tokens IS NULL OR u.total_tokens=u.prompt_tokens+u.completion_tokens+"
        "u.cache_creation_tokens+u.cache_read_tokens) AND (CASE WHEN COALESCE(u.source,'') "
        "IN ('external_harness:claude-code','checkpoint','host_access_action_review',"
        "'host_access_application_review') AND COALESCE(u.model,'') LIKE 'claude-code/%' "
        "THEN 1 WHEN COALESCE(u.source,'') IN ('external_harness:codex','checkpoint') "
        "AND COALESCE(u.model,'') LIKE 'codex/%' AND length(u.id)=24 AND u.id NOT GLOB "
        "'*[^0-9a-f]*' THEN 1 WHEN COALESCE(u.source,'') IN "
        "('host_access_action_review','host_access_application_review') AND "
        "COALESCE(u.model,'') LIKE 'codex/%' THEN 1 WHEN COALESCE(u.source,'')="
        "'conversation' AND COALESCE(u.model,'') LIKE 'external@%' AND "
        "u.created_at>='2026-09-17' THEN 1 WHEN u.cache_creation_tokens=0 AND "
        "u.cache_read_tokens=0 AND COALESCE(u.source,'')<>'external_harness:codex' "
        "AND COALESCE(u.model,'') NOT LIKE 'codex/%' THEN 1 ELSE 0 END)=1\n    "
    ),
    "presence": "SELECT MIN(v.entered_at) FROM presence_visits v",
    "accepted_size_ratings": """
        SELECT MIN(e.created_at)
        FROM plan_node_events e INDEXED BY idx_plan_node_events_created
        JOIN plan_nodes n ON n.id=e.node_id
        WHERE n.kind='task' AND e.new_state IN ('accepted','accepted_with_followup')
          AND (n.size IS NOT NULL AND n.size != 'unknown' OR EXISTS (
            SELECT 1 FROM outcome_events f INDEXED BY idx_measurement_size_rater
            WHERE f.event_name='measurement.size_rating' AND f.retracted_at IS NULL
              AND f.subject_key='plan_node:'||n.id
              AND json_extract(f.attributes,'$.rater') > ''
          ))
    """,
    "limit_hits": """
        SELECT MIN(started_at) FROM active_turns WHERE status > ''
    """,
}
_MIGRATION_LEGACY_SOURCES = {
    "outcomes": ("accepted_outcomes",),
    "first_try": (
        "accepted_outcomes",
        "rejected_outcomes",
        "released_bad_attempts",
        "failed_outcomes",
        "outcome_corrections",
    ),
    "rejections_per_outcome": ("rejected_outcomes",),
    "asks_per_outcome": ("raised_decisions", "user_holds"),
    "decisions": ("answered_decisions",),
    "tokens": ("priceable_usage",),
    "presence": ("presence",),
    "estimated_human_hours_per_active_hour": ("accepted_size_ratings",),
    "limit_hits": ("limit_hits",),
}
_MIGRATION_INDEXES = {
    "threads": """
    CREATE INDEX IF NOT EXISTS idx_measurement_thread_children
        ON threads(parent_thread_id, user_id) WHERE parent_thread_id IS NOT NULL""",
    "messages": """
    CREATE INDEX IF NOT EXISTS idx_measurement_human_messages_time
        ON messages(thread_id, created_at, id) WHERE role='user'""",
    "plan_node_events": """
    CREATE INDEX IF NOT EXISTS idx_measurement_rejections_time
        ON plan_node_events(created_at, node_id) WHERE event='review_rejected'""",
    "llm_usage": (
        "\n    CREATE INDEX IF NOT EXISTS idx_llm_usage_priceable_measurement_start "
        "ON llm_usage(created_at)\n    WHERE typeof(prompt_tokens)='integer' AND "
        "prompt_tokens>=0 AND typeof(completion_tokens)='integer' AND "
        "completion_tokens>=0 AND typeof(cache_creation_tokens)='integer' AND "
        "cache_creation_tokens>=0 AND typeof(cache_read_tokens)='integer' AND "
        "cache_read_tokens>=0 AND (total_tokens IS NULL OR "
        "total_tokens=prompt_tokens+completion_tokens+cache_creation_tokens+"
        "cache_read_tokens) AND (CASE WHEN COALESCE(source,'') IN "
        "('external_harness:claude-code','checkpoint','host_access_action_review',"
        "'host_access_application_review') AND COALESCE(model,'') LIKE "
        "'claude-code/%' THEN 1 WHEN COALESCE(source,'') IN "
        "('external_harness:codex','checkpoint') AND COALESCE(model,'') LIKE "
        "'codex/%' AND length(id)=24 AND id NOT GLOB '*[^0-9a-f]*' THEN 1 WHEN "
        "COALESCE(source,'') IN ('host_access_action_review',"
        "'host_access_application_review') AND COALESCE(model,'') LIKE "
        "'codex/%' THEN 1 WHEN COALESCE(source,'')='conversation' AND "
        "COALESCE(model,'') LIKE 'external@%' AND created_at>='2026-09-17' THEN 1 "
        "WHEN cache_creation_tokens=0 AND cache_read_tokens=0 AND "
        "COALESCE(source,'')<>'external_harness:codex' AND COALESCE(model,'') "
        "NOT LIKE 'codex/%' THEN 1 ELSE 0 END)=1"
    ),
    "artifact_identities": """
    CREATE INDEX IF NOT EXISTS idx_measurement_decisions_thread
        ON artifact_identities(json_extract(metadata, '$.plan_decision.thread_id'))
        WHERE integration='local' AND type='plan-decision' AND json_valid(metadata)""",
    "decision_raised": """
    CREATE INDEX IF NOT EXISTS idx_measurement_decisions_raised
        ON artifact_identities(json_extract(metadata, '$.plan_decision.raised_at'))
        WHERE integration='local' AND type='plan-decision' AND json_valid(metadata)""",
    "decision_answered": """
    CREATE INDEX IF NOT EXISTS idx_measurement_decisions_answered
        ON artifact_identities(json_extract(metadata, '$.plan_decision.answered_at'))
        WHERE integration='local' AND type='plan-decision' AND json_valid(metadata)""",
    "size_rater": """
    CREATE INDEX IF NOT EXISTS idx_measurement_size_rater
        ON outcome_events(json_extract(attributes, '$.rater'), subject_key)
        WHERE event_name='measurement.size_rating' AND retracted_at IS NULL
          AND json_extract(attributes, '$.rater') > ''""",
}

def _migration_first_rows_query(sources: tuple[str, ...]) -> str:
    return "SELECT " + ", ".join(f"({_MIGRATION_FIRST_ROW_SQL[source]}) AS {source}" for source in sources)


def _has_columns(schema: dict[str, set[str]], required: dict[str, set[str]]) -> bool:
    return all(columns <= schema.get(table, set()) for table, columns in required.items())


def _record_existing_starts(conn, schema: dict[str, set[str]]) -> dict[str, str]:
    """Seed boundaries only for source groups whose legacy shape is present."""
    required_columns = {
        "outcomes": {"plan_node_events": {"created_at", "node_id", "new_state"}, "plan_nodes": {"id", "kind"}},
        "first_try": {
            "plan_node_events": {"created_at", "node_id", "event", "new_state"},
            "plan_nodes": {"id", "kind"},
            "plan_node_attempts": {"released_at", "release_reason"},
        },
        "rejections_per_outcome": {"plan_node_events": {"created_at", "event"}},
        "asks_per_outcome": {
            "artifact_identities": {"metadata", "integration", "type"},
            "plan_node_holds": {"created_at", "owner_kind"},
        },
        "decisions": {"artifact_identities": {"metadata", "integration", "type"}},
        "tokens": {"llm_usage": {"id", "model", "source", "created_at", "prompt_tokens", "completion_tokens", "cache_creation_tokens", "cache_read_tokens", "total_tokens"}},
        "presence": {"presence_visits": {"entered_at"}},
        "estimated_human_hours_per_active_hour": {
            "plan_node_events": {"created_at", "node_id", "new_state"},
            "plan_nodes": {"id", "kind", "size"},
            "outcome_events": {"event_name", "retracted_at", "subject_key", "attributes"},
        },
        "limit_hits": {"active_turns": {"started_at", "status"}},
    }
    groups = tuple(group for group, required in required_columns.items() if _has_columns(schema, required))
    if not groups:
        return {}
    sources = tuple(dict.fromkeys(source for group in groups for source in _MIGRATION_LEGACY_SOURCES[group]))
    row = conn.execute(_migration_first_rows_query(sources)).fetchone()
    first = dict(zip(sources, row)) if row else {}
    values: dict[str, str] = {}
    for group in groups:
        if value := max(
            (str(first_value) for source in _MIGRATION_LEGACY_SOURCES[group] if (first_value := first.get(source))),
            default=None,
        ):
            values[group] = value
    conn.executemany(
        "INSERT OR IGNORE INTO measure_breaks(period,kind,metric_group,reason) VALUES (?,?,?,?)",
        [
            (value, "start", group, "first recorded on this computer")
            for group, value in values.items()
        ],
    )
    return values


def _record_legacy_marks(conn, starts: dict[str, str]) -> None:
    """Keep only historical marks that describe this installation's own data."""
    rows: list[tuple[str, str, str | None, str]] = []
    if (outcomes_start := starts.get("outcomes")) and outcomes_start < "2026-08-31":
        rows.append(("2026-08-31", "world", None, "tasks got about 5 times smaller"))
    if (tokens_start := starts.get("tokens")) and tokens_start < "2026-09-07":
        rows.append(("2026-09-07", "source", "tokens", "token totals cover every agent from Sep 7, 2026 on"))
    conn.executemany(
        "INSERT OR IGNORE INTO measure_breaks(period,kind,metric_group,reason) VALUES (?,?,?,?)",
        rows,
    )


def migrate_146_to_147(conn) -> None:
    """Create immutable Measurement boundaries from this migration's frozen upgrade-time SQL."""
    conn.executescript(MEASUREMENT_BREAKS_SCHEMA_SQL)

    tables = [str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    schema = {
        table: {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
        for table in tables
    }
    index_requirements = {
        "threads": {"threads": {"parent_thread_id", "user_id"}},
        "messages": {"messages": {"thread_id", "created_at", "id", "role"}},
        "plan_node_events": {"plan_node_events": {"created_at", "node_id", "event", "new_state"}},
        "llm_usage": {"llm_usage": {"id", "model", "source", "created_at", "prompt_tokens", "completion_tokens", "cache_creation_tokens", "cache_read_tokens", "total_tokens"}},
        "artifact_identities": {"artifact_identities": {"metadata", "integration", "type"}},
        "decision_raised": {"artifact_identities": {"metadata", "integration", "type"}},
        "decision_answered": {"artifact_identities": {"metadata", "integration", "type"}},
        "size_rater": {"outcome_events": {"event_name", "retracted_at", "attributes", "subject_key"}},
    }
    for name, sql in _MIGRATION_INDEXES.items():
        if _has_columns(schema, index_requirements[name]):
            conn.execute(sql)
    _record_legacy_marks(conn, _record_existing_starts(conn, schema))
