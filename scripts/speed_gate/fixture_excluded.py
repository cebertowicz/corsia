"""Tables the fixture deliberately does not fill, each with its reason.

The build seeds every table the volume census (``fixture_spec``) gives a figure for. A table in the schema
that it neither seeds nor lists here fails the build: a new table must be given a census figure or be listed
with a reason, so a read over it is never measured on an empty table by accident. Counts for the small
configuration tables are the production census of 2026-10-08.
"""

from __future__ import annotations

FTS_SHADOW = "FTS5 shadow table: SQLite maintains it from the rows of the indexed table"
DERIVED_ROLLUP = ("derived from llm_usage by the application's own rollup refresh, which the harness drains "
                  "inside the measured window")
NOT_IN_CENSUS = "absent from the production census's table list, so there is no volume to reproduce"


def _small(count: int) -> str:
    return (f"{count} row(s) in production: the fixture keeps what init_db and the harness's test accounts "
            "create, not a copy of production's")


#: table -> reason it is not seeded.
EXCLUDED: dict[str, str] = {
    **{table: FTS_SHADOW for table in (
        "checkpoint_entry_search_fts_config", "checkpoint_entry_search_fts_data",
        "checkpoint_entry_search_fts_docsize", "checkpoint_entry_search_fts_idx",
        "checkpoints_fts_config", "checkpoints_fts_data", "checkpoints_fts_docsize", "checkpoints_fts_idx",
        "messages_fts_config", "messages_fts_data", "messages_fts_docsize", "messages_fts_idx")},
    **{table: DERIVED_ROLLUP for table in (
        "llm_usage_hourly", "llm_usage_hourly_dirty", "llm_usage_hourly_state")},
    "models": _small(221),
    "settings": _small(106),
    "auth_tokens": _small(11),
    "model_pricing": _small(10),
    "network_servers": _small(7),
    "vendor_accounts": _small(7),
    "connections": _small(3),
    "provider_policies": _small(2),
    "project_policy_overrides": _small(1),
    "system_bulletin_identity": _small(1),
    "outcome_event_counters": _small(1),
    "users": _small(1),
    **{table: NOT_IN_CENSUS for table in (
        "action_drafts", "artifact_relocation_intents", "connected_app_audit", "connected_app_reach",
        "connected_apps", "connection_rules", "doc_feedback_edit_items", "durable_write_intents",
        "email_screens", "key_store_entries", "key_write_attempts", "lesson_placement_checks",
        "limit_observations", "managed_process_retirements", "plan_ask_deferrals",
        "plan_markdown_import_issues", "plan_markdown_imports",
        "plan_status_dirty", "provider_availability", "reviewer_gate_approval",
        "vendor_account_billing_facts", "vendor_account_billing_revisions")},
}

#: table -> (rows the fixture ends up with, why that is below its census target). The build compares every
#: table's stored rows with ``fixture_spec.target_rows``: a table below target fails the build unless it is
#: listed here with its exact actual count, so a shortfall is never an accident and a stale entry fails too.
SHORT: dict[str, tuple[int, str]] = {
    "plan_liveness_wakes": (553, "one row per thread (thread_id is UNIQUE) and the fixture has 553 threads, "
                                 "so it cannot hold the census's 966 at this scale"),
}
