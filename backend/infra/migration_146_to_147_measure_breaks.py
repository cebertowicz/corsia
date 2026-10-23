"""Schema 146 -> 147: persist Measurement breaks that source rows cannot record."""

from __future__ import annotations

from infra.measurement_schema import MEASUREMENT_BREAKS_SCHEMA_SQL, MEASUREMENT_INDEXES
from infra.measurement_starts import SOURCE_START_SQL, record_existing_model_versions, source_starts_query


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
        "asks_per_outcome": {"artifact_identities": {"metadata", "integration", "type"}},
        "asks_hold": {"plan_node_holds": {"created_at", "owner_kind"}},
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
    row = conn.execute(source_starts_query(groups)).fetchone()
    values = {group: str(value) for group, value in zip(groups, row) if value} if row else {}
    conn.executemany(
        "INSERT OR IGNORE INTO measure_breaks(period,kind,metric_group,reason) VALUES (?,?,?,?)",
        [
            (value, "source" if group == "asks_hold" else "start", group,
             "user-owned holds first recorded" if group == "asks_hold" else "instrument source first recorded")
            for group, value in values.items()
        ],
    )
    return values


def _record_legacy_marks(conn, starts: dict[str, str]) -> None:
    """Keep only historical marks that describe this installation's own data."""
    rows: list[tuple[str, str, str | None, str]] = []
    if (outcomes_start := starts.get("outcomes")) and outcomes_start < "2026-08-31":
        rows.append(("2026-08-31", "world", None, "plan nodes got about 5 times smaller"))
    if (tokens_start := starts.get("tokens")) and tokens_start < "2026-09-07":
        rows.append(("2026-09-07", "source", "tokens", "token totals cover every harness from this week"))
    conn.executemany(
        "INSERT OR IGNORE INTO measure_breaks(period,kind,metric_group,reason) VALUES (?,?,?,?)",
        rows,
    )


def migrate_146_to_147(conn) -> None:
    """Create immutable Measurement boundaries and seed legacy-install history."""
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
        "plan_node_holds": {"plan_node_holds": {"created_at", "owner_kind"}},
        "llm_usage": {"llm_usage": {"id", "model", "source", "created_at", "prompt_tokens", "completion_tokens", "cache_creation_tokens", "cache_read_tokens", "total_tokens"}},
        "artifact_identities": {"artifact_identities": {"metadata", "integration", "type"}},
        "decision_raised": {"artifact_identities": {"metadata", "integration", "type"}},
        "decision_answered": {"artifact_identities": {"metadata", "integration", "type"}},
        "first_try_failed": {"plan_node_events": {"created_at", "new_state"}},
        "first_try_corrections": {"plan_node_events": {"created_at", "event"}},
        "first_try_released_bad": {"plan_node_attempts": {"released_at", "release_reason"}},
        "size_rater": {"outcome_events": {"event_name", "retracted_at", "attributes", "subject_key"}},
    }
    for name, sql in MEASUREMENT_INDEXES.items():
        if _has_columns(schema, index_requirements[name]):
            conn.execute(sql)
    conn.execute("DROP INDEX IF EXISTS idx_measurement_presence_start")
    conn.execute("DROP INDEX IF EXISTS idx_measurement_active_turns_start")
    _record_legacy_marks(conn, _record_existing_starts(conn, schema))
    if _has_columns(schema, {
        "outcome_events": {"event_name", "retracted_at", "subject_key", "attributes"},
        "plan_nodes": {"id"}, "plan_node_events": {"node_id", "created_at", "new_state"},
    }):
        record_existing_model_versions(conn)
