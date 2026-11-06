# Data model

Generated reference for the primary, document, log, and analytics stores.

Generate with `.venv/bin/python scripts/docs/generate_data_model.py`; add `--check` to compare the generated pages. The backend's locked dependencies are required.

SQLite full-text search creates shadow tables; SQLite creates `sqlite_sequence`. Relationships shown in a diagram connect tables in that diagram; column tables list all declared foreign keys. The measurement tables are temporary and are created on a primary connection when needed.

| Subsystem | Page | Source modules |
|---|---|---|
| Accounts and connections | [Accounts and connections](accounts-and-connections.md) | `backend/infra/accounts_schema.py`, `backend/infra/action_drafts_schema.py`, `backend/infra/connected_accounts_schema.py`, `backend/infra/connected_apps_schema.py`, `backend/infra/connection_schema.py`, `backend/infra/email_screens_schema.py`, `backend/infra/key_store_schema.py`, `backend/infra/vendor_account_schema.py` |
| Analytics store | [Analytics store](analytics-store.md) | `backend/infra/analytics_db.py` |
| Context and memory | [Context and memory](context-and-memory.md) | `backend/infra/checkpoint_schema.py`, `backend/infra/context_change_schema.py`, `backend/infra/continuity_schema.py`, `backend/infra/continuity_staging.py`, `backend/infra/fact_extraction_schema.py`, `backend/infra/lesson_placement_check_schema.py`, `backend/infra/schema.py` |
| Document store | [Document store](document-store.md) | `backend/doceditor/live_store.py` |
| Documents and artifacts | [Documents and artifacts](documents-and-artifacts.md) | `backend/infra/artifact_identity_schema.py`, `backend/infra/artifact_relocation_schema.py`, `backend/infra/doc_comments_schema.py`, `backend/infra/doc_feedback_schema.py`, `backend/infra/doc_review_schema.py`, `backend/infra/schema.py` |
| Events and webhooks | [Events and webhooks](events-and-webhooks.md) | `backend/infra/bulletin_schema.py`, `backend/infra/event_intake_schema.py`, `backend/infra/event_rule_schema.py`, `backend/infra/event_watch_schema.py`, `backend/infra/schema.py`, `backend/infra/webhook_delivery_schema.py` |
| Host access | [Host access](host-access.md) | `backend/infra/host_access_schema.py` |
| Log store | [Log store](log-store.md) | `backend/infra/otel_logs/store.py` |
| Plans | [Plans](plans.md) | `backend/infra/plan_events_schema.py`, `backend/infra/plan_liveness_schema.py`, `backend/infra/plan_markdown_import_schema.py`, `backend/infra/plan_pane_views_schema.py`, `backend/infra/plan_schema.py`, `backend/infra/plan_status_schema.py`, `backend/infra/unattributed_dispatch_schema.py` |
| Providers and runtime | [Providers and runtime](providers-and-runtime.md) | `backend/infra/limit_history_schema.py`, `backend/infra/managed_process_schema.py`, `backend/infra/provider_availability_schema.py`, `backend/infra/schema.py` |
| Settings and policy | [Settings and policy](settings-and-policy.md) | `backend/infra/project_policy_schema.py`, `backend/infra/reviewer_gate_schema.py`, `backend/infra/schema.py`, `backend/infra/settings_schema.py`, `backend/infra/vendor_policy_schema.py` |
| Store internals | [Store internals](store-internals.md) | `SQLite internal`, `backend/infra/db_fts.py` |
| Threads and messages | [Threads and messages](threads-and-messages.md) | `backend/infra/chat_comment_schema.py`, `backend/infra/continuation_schema.py`, `backend/infra/held_replies_schema.py`, `backend/infra/message_schema.py`, `backend/infra/presence_rollup_schema.py`, `backend/infra/presence_schema.py`, `backend/infra/schema.py`, `backend/infra/thread_updates_schema.py` |
| Usage and measurement | [Usage and measurement](usage-and-measurement.md) | `backend/api/measurement_prepared.py`, `backend/infra/measure_keys_schema.py`, `backend/infra/measurement_schema.py`, `backend/infra/outcome_events_schema.py`, `backend/infra/usage_rollup_schema.py`, `backend/infra/usage_schema.py` |
