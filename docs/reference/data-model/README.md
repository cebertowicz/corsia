# Data model

Generated reference for the primary, document, log, and analytics stores.

Generate with `PYTHONPATH=backend .venv/bin/python scripts/docs/generate_data_model.py`; add `--check` to compare the generated pages. The backend's locked dependencies are required.

SQLite full-text search creates shadow tables; SQLite creates `sqlite_sequence`. Relationships shown in a diagram connect tables in that diagram; column tables list all declared foreign keys. The measurement tables are temporary and are created on a primary connection when needed.

Historical migration tables are excluded: `checkpoints_new`, `connected_accounts_new`, `connected_account_audit_new`, `_usage_backfill_bound`, `_usage_backfill_unbound`, and `retired_worker_releases` are migration intermediates; `project_vendor_accounts` was retired.

## Primary store

| Schema module | Reference |
|---|---|
| `SQLite internal` | [primary-sqlite-internal](primary-sqlite-internal.md) |
| `backend/api/measurement_prepared.py` | [primary-measurement-prepared](primary-measurement-prepared.md) |
| `backend/infra/accounts_schema.py` | [primary-accounts-schema](primary-accounts-schema.md) |
| `backend/infra/action_drafts_schema.py` | [primary-action-drafts-schema](primary-action-drafts-schema.md) |
| `backend/infra/artifact_identity_schema.py` | [primary-artifact-identity-schema](primary-artifact-identity-schema.md) |
| `backend/infra/artifact_relocation_schema.py` | [primary-artifact-relocation-schema](primary-artifact-relocation-schema.md) |
| `backend/infra/bulletin_schema.py` | [primary-bulletin-schema](primary-bulletin-schema.md) |
| `backend/infra/chat_comment_schema.py` | [primary-chat-comment-schema](primary-chat-comment-schema.md) |
| `backend/infra/checkpoint_schema.py` | [primary-checkpoint-schema](primary-checkpoint-schema.md) |
| `backend/infra/connected_accounts_schema.py` | [primary-connected-accounts-schema](primary-connected-accounts-schema.md) |
| `backend/infra/connected_apps_schema.py` | [primary-connected-apps-schema](primary-connected-apps-schema.md) |
| `backend/infra/connection_schema.py` | [primary-connection-schema](primary-connection-schema.md) |
| `backend/infra/context_change_schema.py` | [primary-context-change-schema](primary-context-change-schema.md) |
| `backend/infra/continuation_schema.py` | [primary-continuation-schema](primary-continuation-schema.md) |
| `backend/infra/continuity_schema.py` | [primary-continuity-schema](primary-continuity-schema.md) |
| `backend/infra/continuity_staging.py` | [primary-continuity-staging](primary-continuity-staging.md) |
| `backend/infra/db_fts.py` | [primary-db-fts](primary-db-fts.md) |
| `backend/infra/doc_comments_schema.py` | [primary-doc-comments-schema](primary-doc-comments-schema.md) |
| `backend/infra/doc_feedback_schema.py` | [primary-doc-feedback-schema](primary-doc-feedback-schema.md) |
| `backend/infra/doc_review_schema.py` | [primary-doc-review-schema](primary-doc-review-schema.md) |
| `backend/infra/email_screens_schema.py` | [primary-email-screens-schema](primary-email-screens-schema.md) |
| `backend/infra/event_intake_schema.py` | [primary-event-intake-schema](primary-event-intake-schema.md) |
| `backend/infra/event_rule_schema.py` | [primary-event-rule-schema](primary-event-rule-schema.md) |
| `backend/infra/event_watch_schema.py` | [primary-event-watch-schema](primary-event-watch-schema.md) |
| `backend/infra/fact_extraction_schema.py` | [primary-fact-extraction-schema](primary-fact-extraction-schema.md) |
| `backend/infra/held_replies_schema.py` | [primary-held-replies-schema](primary-held-replies-schema.md) |
| `backend/infra/host_access_schema.py` | [primary-host-access-schema](primary-host-access-schema.md) |
| `backend/infra/key_store_schema.py` | [primary-key-store-schema](primary-key-store-schema.md) |
| `backend/infra/lesson_placement_check_schema.py` | [primary-lesson-placement-check-schema](primary-lesson-placement-check-schema.md) |
| `backend/infra/limit_history_schema.py` | [primary-limit-history-schema](primary-limit-history-schema.md) |
| `backend/infra/managed_process_schema.py` | [primary-managed-process-schema](primary-managed-process-schema.md) |
| `backend/infra/measure_keys_schema.py` | [primary-measure-keys-schema](primary-measure-keys-schema.md) |
| `backend/infra/measurement_schema.py` | [primary-measurement-schema](primary-measurement-schema.md) |
| `backend/infra/message_schema.py` | [primary-message-schema](primary-message-schema.md) |
| `backend/infra/outcome_events_schema.py` | [primary-outcome-events-schema](primary-outcome-events-schema.md) |
| `backend/infra/plan_events_schema.py` | [primary-plan-events-schema](primary-plan-events-schema.md) |
| `backend/infra/plan_intent_schema.py` | [primary-plan-intent-schema](primary-plan-intent-schema.md) |
| `backend/infra/plan_liveness_schema.py` | [primary-plan-liveness-schema](primary-plan-liveness-schema.md) |
| `backend/infra/plan_markdown_import_schema.py` | [primary-plan-markdown-import-schema](primary-plan-markdown-import-schema.md) |
| `backend/infra/plan_pane_views_schema.py` | [primary-plan-pane-views-schema](primary-plan-pane-views-schema.md) |
| `backend/infra/plan_schema.py` | [primary-plan-schema](primary-plan-schema.md) |
| `backend/infra/plan_status_schema.py` | [primary-plan-status-schema](primary-plan-status-schema.md) |
| `backend/infra/presence_rollup_schema.py` | [primary-presence-rollup-schema](primary-presence-rollup-schema.md) |
| `backend/infra/presence_schema.py` | [primary-presence-schema](primary-presence-schema.md) |
| `backend/infra/project_policy_schema.py` | [primary-project-policy-schema](primary-project-policy-schema.md) |
| `backend/infra/provider_availability_schema.py` | [primary-provider-availability-schema](primary-provider-availability-schema.md) |
| `backend/infra/reviewer_gate_schema.py` | [primary-reviewer-gate-schema](primary-reviewer-gate-schema.md) |
| `backend/infra/schema.py` | [primary-schema](primary-schema.md) |
| `backend/infra/settings_schema.py` | [primary-settings-schema](primary-settings-schema.md) |
| `backend/infra/thread_updates_schema.py` | [primary-thread-updates-schema](primary-thread-updates-schema.md) |
| `backend/infra/unattributed_dispatch_schema.py` | [primary-unattributed-dispatch-schema](primary-unattributed-dispatch-schema.md) |
| `backend/infra/usage_rollup_schema.py` | [primary-usage-rollup-schema](primary-usage-rollup-schema.md) |
| `backend/infra/usage_schema.py` | [primary-usage-schema](primary-usage-schema.md) |
| `backend/infra/vendor_account_schema.py` | [primary-vendor-account-schema](primary-vendor-account-schema.md) |
| `backend/infra/vendor_policy_schema.py` | [primary-vendor-policy-schema](primary-vendor-policy-schema.md) |
| `backend/infra/webhook_delivery_schema.py` | [primary-webhook-delivery-schema](primary-webhook-delivery-schema.md) |

## Document store

| Schema module | Reference |
|---|---|
| `backend/doceditor/live_store.py` | [document-live-store](document-live-store.md) |

## Logs store

| Schema module | Reference |
|---|---|
| `backend/infra/otel_logs/store.py` | [logs-store](logs-store.md) |

## Analytics store

| Schema module | Reference |
|---|---|
| `backend/infra/analytics_db.py` | [analytics-analytics-db](analytics-analytics-db.md) |

## Table to schema module

| Store | Table | Module |
|---|---|---|
| analytics | `product_events` | `backend/infra/analytics_db.py` |
| analytics | `request_slow` | `backend/infra/analytics_db.py` |
| analytics | `request_timing_daily` | `backend/infra/analytics_db.py` |
| document | `live` | `backend/doceditor/live_store.py` |
| document | `meta` | `backend/doceditor/live_store.py` |
| document | `mirror` | `backend/doceditor/live_store.py` |
| document | `pending` | `backend/doceditor/live_store.py` |
| document | `publish` | `backend/doceditor/live_store.py` |
| logs | `log_records` | `backend/infra/otel_logs/store.py` |
| logs | `store_meta` | `backend/infra/otel_logs/store.py` |
| primary | `sqlite_sequence` | `SQLite internal` |
| primary | `measurement_outcome_nodes` | `backend/api/measurement_prepared.py` |
| primary | `measurement_outcome_periods` | `backend/api/measurement_prepared.py` |
| primary | `measurement_root_difficulty_periods` | `backend/api/measurement_prepared.py` |
| primary | `measurement_root_outcome_periods` | `backend/api/measurement_prepared.py` |
| primary | `measurement_scope_roots` | `backend/api/measurement_prepared.py` |
| primary | `measurement_scope_threads` | `backend/api/measurement_prepared.py` |
| primary | `measurement_task_chain` | `backend/api/measurement_prepared.py` |
| primary | `auth_tokens` | `backend/infra/accounts_schema.py` |
| primary | `users` | `backend/infra/accounts_schema.py` |
| primary | `action_drafts` | `backend/infra/action_drafts_schema.py` |
| primary | `artifact_identities` | `backend/infra/artifact_identity_schema.py` |
| primary | `artifact_relocation_intents` | `backend/infra/artifact_relocation_schema.py` |
| primary | `system_bulletin_identity` | `backend/infra/bulletin_schema.py` |
| primary | `system_bulletins` | `backend/infra/bulletin_schema.py` |
| primary | `chat_comment_answers` | `backend/infra/chat_comment_schema.py` |
| primary | `chat_comment_views` | `backend/infra/chat_comment_schema.py` |
| primary | `chat_comments` | `backend/infra/chat_comment_schema.py` |
| primary | `checkpoint_entry_search` | `backend/infra/checkpoint_schema.py` |
| primary | `checkpoints` | `backend/infra/checkpoint_schema.py` |
| primary | `connected_account_audit` | `backend/infra/connected_accounts_schema.py` |
| primary | `connected_account_reach` | `backend/infra/connected_accounts_schema.py` |
| primary | `connected_accounts` | `backend/infra/connected_accounts_schema.py` |
| primary | `connected_app_audit` | `backend/infra/connected_apps_schema.py` |
| primary | `connected_app_reach` | `backend/infra/connected_apps_schema.py` |
| primary | `connected_apps` | `backend/infra/connected_apps_schema.py` |
| primary | `connection_rules` | `backend/infra/connection_schema.py` |
| primary | `connections` | `backend/infra/connection_schema.py` |
| primary | `context_change_journal` | `backend/infra/context_change_schema.py` |
| primary | `automation_schedules` | `backend/infra/continuation_schema.py` |
| primary | `continuation_triggers` | `backend/infra/continuation_schema.py` |
| primary | `continuity_entries` | `backend/infra/continuity_schema.py` |
| primary | `continuity_store` | `backend/infra/continuity_schema.py` |
| primary | `checkpoint_staged_edits` | `backend/infra/continuity_staging.py` |
| primary | `checkpoint_entry_search_fts` | `backend/infra/db_fts.py` |
| primary | `checkpoint_entry_search_fts_config` | `backend/infra/db_fts.py` |
| primary | `checkpoint_entry_search_fts_data` | `backend/infra/db_fts.py` |
| primary | `checkpoint_entry_search_fts_docsize` | `backend/infra/db_fts.py` |
| primary | `checkpoint_entry_search_fts_idx` | `backend/infra/db_fts.py` |
| primary | `checkpoints_fts` | `backend/infra/db_fts.py` |
| primary | `checkpoints_fts_config` | `backend/infra/db_fts.py` |
| primary | `checkpoints_fts_data` | `backend/infra/db_fts.py` |
| primary | `checkpoints_fts_docsize` | `backend/infra/db_fts.py` |
| primary | `checkpoints_fts_idx` | `backend/infra/db_fts.py` |
| primary | `messages_fts` | `backend/infra/db_fts.py` |
| primary | `messages_fts_config` | `backend/infra/db_fts.py` |
| primary | `messages_fts_data` | `backend/infra/db_fts.py` |
| primary | `messages_fts_docsize` | `backend/infra/db_fts.py` |
| primary | `messages_fts_idx` | `backend/infra/db_fts.py` |
| primary | `doc_comments` | `backend/infra/doc_comments_schema.py` |
| primary | `doc_comment_thread_views` | `backend/infra/doc_feedback_schema.py` |
| primary | `doc_feedback_batch_items` | `backend/infra/doc_feedback_schema.py` |
| primary | `doc_feedback_batches` | `backend/infra/doc_feedback_schema.py` |
| primary | `doc_feedback_edit_items` | `backend/infra/doc_feedback_schema.py` |
| primary | `doc_review_requests` | `backend/infra/doc_review_schema.py` |
| primary | `email_screens` | `backend/infra/email_screens_schema.py` |
| primary | `event_intake_records` | `backend/infra/event_intake_schema.py` |
| primary | `event_route_decisions` | `backend/infra/event_rule_schema.py` |
| primary | `event_rules` | `backend/infra/event_rule_schema.py` |
| primary | `self_caused_events` | `backend/infra/event_rule_schema.py` |
| primary | `expected_event_watches` | `backend/infra/event_watch_schema.py` |
| primary | `fact_extraction_runs` | `backend/infra/fact_extraction_schema.py` |
| primary | `held_replies` | `backend/infra/held_replies_schema.py` |
| primary | `host_access_action_reviews` | `backend/infra/host_access_schema.py` |
| primary | `host_access_leases` | `backend/infra/host_access_schema.py` |
| primary | `key_store_entries` | `backend/infra/key_store_schema.py` |
| primary | `key_write_attempts` | `backend/infra/key_store_schema.py` |
| primary | `lesson_placement_checks` | `backend/infra/lesson_placement_check_schema.py` |
| primary | `limit_observations` | `backend/infra/limit_history_schema.py` |
| primary | `managed_process_handles` | `backend/infra/managed_process_schema.py` |
| primary | `managed_process_retirements` | `backend/infra/managed_process_schema.py` |
| primary | `measure_keys` | `backend/infra/measure_keys_schema.py` |
| primary | `measure_breaks` | `backend/infra/measurement_schema.py` |
| primary | `messages` | `backend/infra/message_schema.py` |
| primary | `measure_definitions` | `backend/infra/outcome_events_schema.py` |
| primary | `outcome_event_counters` | `backend/infra/outcome_events_schema.py` |
| primary | `outcome_events` | `backend/infra/outcome_events_schema.py` |
| primary | `plan_events` | `backend/infra/plan_events_schema.py` |
| primary | `plan_node_intent_words` | `backend/infra/plan_intent_schema.py` |
| primary | `plan_liveness_wakes` | `backend/infra/plan_liveness_schema.py` |
| primary | `plan_markdown_import_issues` | `backend/infra/plan_markdown_import_schema.py` |
| primary | `plan_markdown_imports` | `backend/infra/plan_markdown_import_schema.py` |
| primary | `plan_pane_views` | `backend/infra/plan_pane_views_schema.py` |
| primary | `plan_ask_deferrals` | `backend/infra/plan_schema.py` |
| primary | `plan_mutation_receipts` | `backend/infra/plan_schema.py` |
| primary | `plan_node_artifact_links` | `backend/infra/plan_schema.py` |
| primary | `plan_node_attempts` | `backend/infra/plan_schema.py` |
| primary | `plan_node_dependencies` | `backend/infra/plan_schema.py` |
| primary | `plan_node_events` | `backend/infra/plan_schema.py` |
| primary | `plan_node_holds` | `backend/infra/plan_schema.py` |
| primary | `plan_nodes` | `backend/infra/plan_schema.py` |
| primary | `plan_tool_dispatches` | `backend/infra/plan_schema.py` |
| primary | `plans` | `backend/infra/plan_schema.py` |
| primary | `plan_status_dirty` | `backend/infra/plan_status_schema.py` |
| primary | `presence_focus_rollup` | `backend/infra/presence_rollup_schema.py` |
| primary | `presence_focus_rollup_state` | `backend/infra/presence_rollup_schema.py` |
| primary | `presence_focus_set_members` | `backend/infra/presence_rollup_schema.py` |
| primary | `presence_focus_sets` | `backend/infra/presence_rollup_schema.py` |
| primary | `presence_visits` | `backend/infra/presence_schema.py` |
| primary | `thread_presence` | `backend/infra/presence_schema.py` |
| primary | `project_inference_policies` | `backend/infra/project_policy_schema.py` |
| primary | `project_repo_policies` | `backend/infra/project_policy_schema.py` |
| primary | `provider_availability` | `backend/infra/provider_availability_schema.py` |
| primary | `reviewer_gate_approval` | `backend/infra/reviewer_gate_schema.py` |
| primary | `reviewer_gate_state` | `backend/infra/reviewer_gate_schema.py` |
| primary | `active_turns` | `backend/infra/schema.py` |
| primary | `artifact_reconcile_receipts` | `backend/infra/schema.py` |
| primary | `artifact_registration_events` | `backend/infra/schema.py` |
| primary | `artifact_registrations` | `backend/infra/schema.py` |
| primary | `context_bootloader_events` | `backend/infra/schema.py` |
| primary | `dead_letter_events` | `backend/infra/schema.py` |
| primary | `durable_write_intents` | `backend/infra/schema.py` |
| primary | `model_profiles` | `backend/infra/schema.py` |
| primary | `models` | `backend/infra/schema.py` |
| primary | `network_server_endpoints` | `backend/infra/schema.py` |
| primary | `network_servers` | `backend/infra/schema.py` |
| primary | `projects` | `backend/infra/schema.py` |
| primary | `restart_wake_registrations` | `backend/infra/schema.py` |
| primary | `self_restart_requests` | `backend/infra/schema.py` |
| primary | `threads` | `backend/infra/schema.py` |
| primary | `settings` | `backend/infra/settings_schema.py` |
| primary | `thread_updates` | `backend/infra/thread_updates_schema.py` |
| primary | `plan_unattributed_dispatches` | `backend/infra/unattributed_dispatch_schema.py` |
| primary | `llm_usage_hourly` | `backend/infra/usage_rollup_schema.py` |
| primary | `llm_usage_hourly_dirty` | `backend/infra/usage_rollup_schema.py` |
| primary | `llm_usage_hourly_state` | `backend/infra/usage_rollup_schema.py` |
| primary | `context_manifests` | `backend/infra/usage_schema.py` |
| primary | `guidance_loads` | `backend/infra/usage_schema.py` |
| primary | `llm_usage` | `backend/infra/usage_schema.py` |
| primary | `model_pricing` | `backend/infra/usage_schema.py` |
| primary | `thread_vendor_accounts` | `backend/infra/vendor_account_schema.py` |
| primary | `vendor_account_billing_facts` | `backend/infra/vendor_account_schema.py` |
| primary | `vendor_account_billing_revisions` | `backend/infra/vendor_account_schema.py` |
| primary | `vendor_accounts` | `backend/infra/vendor_account_schema.py` |
| primary | `project_policy_overrides` | `backend/infra/vendor_policy_schema.py` |
| primary | `provider_policies` | `backend/infra/vendor_policy_schema.py` |
| primary | `thread_vendor_account_holds` | `backend/infra/vendor_policy_schema.py` |
| primary | `webhook_deliveries` | `backend/infra/webhook_delivery_schema.py` |
