# Usage and measurement

These tables store model usage, pricing and measurement data.

## Tables

### `context_manifests`

Source: `backend/infra/usage_schema.py`.

| Column | Type | Primary key | NOT NULL | Default | References |
|---|---|---|---|---|---|
| `id` | TEXT | Yes | No | — | — |
| `harness` | TEXT | No | Yes | — | — |
| `selector` | TEXT | No | No | — | — |
| `resolved_model` | TEXT | No | No | — | — |
| `resolved_model_source` | TEXT | No | No | — | — |
| `entries_json` | TEXT | No | Yes | — | — |
| `entry_count` | INTEGER | No | Yes | — | — |
| `created_at` | TEXT | No | Yes | — | — |

Indexes: `sqlite_autoindex_context_manifests_1` (id, unique).

### `guidance_loads`

Source: `backend/infra/usage_schema.py`.

| Column | Type | Primary key | NOT NULL | Default | References |
|---|---|---|---|---|---|
| `id` | TEXT | Yes | No | — | — |
| `thread_id` | TEXT | No | No | — | — |
| `turn_id` | TEXT | No | No | — | — |
| `path` | TEXT | No | Yes | — | — |
| `raw_sha256` | TEXT | No | Yes | — | — |
| `git_blob_oid` | TEXT | No | Yes | — | — |
| `delivered_sha256` | TEXT | No | Yes | — | — |
| `byte_count` | INTEGER | No | Yes | — | — |
| `partial` | INTEGER | No | Yes | — | — |
| `created_at` | TEXT | No | Yes | — | — |
| `node_id` | TEXT | No | No | — | — |
| `variant` | TEXT | No | No | — | — |
| `checkpoint_generation_id` | TEXT | No | No | — | — |
| `lesson_id` | TEXT | No | No | — | — |
| `lesson_behavioral_sha256` | TEXT | No | No | — | — |
| `omission_reason` | TEXT | No | No | — | — |

Indexes: `idx_guidance_loads_lesson_node` (lesson_id, lesson_behavioral_sha256, node_id), `idx_guidance_loads_trial_thread_time` (thread_id, created_at, lesson_id, lesson_behavioral_sha256, id), `idx_guidance_loads_turn` (turn_id), `sqlite_autoindex_guidance_loads_1` (id, unique).

### `llm_usage`

Source: `backend/infra/usage_schema.py`.

| Column | Type | Primary key | NOT NULL | Default | References |
|---|---|---|---|---|---|
| `id` | TEXT | Yes | No | — | — |
| `thread_id` | TEXT | No | Yes | — | — |
| `turn_id` | TEXT | No | No | — | — |
| `vendor_account_id` | TEXT | No | No | — | vendor_accounts.id |
| `model` | TEXT | No | Yes | — | — |
| `prompt_tokens` | INTEGER | No | No | 0 | — |
| `completion_tokens` | INTEGER | No | No | 0 | — |
| `cache_creation_tokens` | INTEGER | No | No | 0 | — |
| `cache_read_tokens` | INTEGER | No | No | 0 | — |
| `total_tokens` | INTEGER | No | No | 0 | — |
| `cost_usd` | REAL | No | No | 0.0 | — |
| `ttft_ms` | INTEGER | No | No | — | — |
| `plan_id` | TEXT | No | No | — | — |
| `plan_node_id` | TEXT | No | No | — | — |
| `plan_attempt_id` | TEXT | No | No | — | — |
| `plan_commitment_id` | TEXT | No | No | — | — |
| `plan_usage_role` | TEXT | No | No | — | — |
| `source` | TEXT | No | Yes | 'conversation' | — |
| `created_at` | TEXT | No | Yes | — | — |
| `context_manifest_id` | TEXT | No | No | — | — |
| `reasoning_effort` | TEXT | No | No | — | — |

Indexes: `idx_llm_usage_commitment` (plan_commitment_id, created_at), `idx_llm_usage_context_manifest` (context_manifest_id), `idx_llm_usage_created_at` (created_at), `idx_llm_usage_model` (model, created_at), `idx_llm_usage_plan_node` (plan_node_id), `idx_llm_usage_priceable_measurement_start` (created_at), `idx_llm_usage_session` (thread_id, created_at), `idx_llm_usage_source` (source, created_at), `idx_llm_usage_turn` (turn_id), `sqlite_autoindex_llm_usage_1` (id, unique).

### `llm_usage_hourly`

Source: `backend/infra/usage_rollup_schema.py`.

| Column | Type | Primary key | NOT NULL | Default | References |
|---|---|---|---|---|---|
| `hour` | TEXT | No | Yes | — | — |
| `thread_id` | TEXT | No | No | — | — |
| `model` | TEXT | No | No | — | — |
| `source` | TEXT | No | No | — | — |
| `context_manifest_id` | TEXT | No | No | — | — |
| `vendor_account_id` | TEXT | No | No | — | — |
| `admission` | TEXT | No | Yes | — | — |
| `input_tier` | TEXT | No | Yes | — | — |
| `idle_hours` | INTEGER | No | Yes | — | — |
| `records` | INTEGER | No | Yes | — | — |
| `prompt_tokens` | INTEGER | No | No | — | — |
| `completion_tokens` | INTEGER | No | No | — | — |
| `cache_creation_tokens` | INTEGER | No | No | — | — |
| `cache_read_tokens` | INTEGER | No | No | — | — |
| `total_tokens` | INTEGER | No | No | — | — |
| `fresh_tokens` | INTEGER | No | No | — | — |
| `fresh_unknown_records` | INTEGER | No | No | — | — |
| `cost_usd` | REAL | No | No | — | — |
| `cost_records` | INTEGER | No | Yes | — | — |
| `first_at` | TEXT | No | Yes | — | — |

Indexes: `idx_llm_usage_hourly_hour` (hour, thread_id).

### `llm_usage_hourly_dirty`

Source: `backend/infra/usage_rollup_schema.py`.

| Column | Type | Primary key | NOT NULL | Default | References |
|---|---|---|---|---|---|
| `hour` | TEXT | Yes | No | — | — |
| `seq` | INTEGER | No | Yes | 1 | — |

Indexes: `sqlite_autoindex_llm_usage_hourly_dirty_1` (hour, unique).

### `llm_usage_hourly_state`

Source: `backend/infra/usage_rollup_schema.py`.

| Column | Type | Primary key | NOT NULL | Default | References |
|---|---|---|---|---|---|
| `id` | INTEGER | Yes | No | — | — |
| `definition` | TEXT | No | Yes | — | — |
| `built_through` | TEXT | No | Yes | — | — |

Indexes: None.

### `measure_breaks`

Source: `backend/infra/measurement_schema.py`.

| Column | Type | Primary key | NOT NULL | Default | References |
|---|---|---|---|---|---|
| `period` | TEXT | Yes | Yes | — | — |
| `kind` | TEXT | Yes | Yes | — | — |
| `metric_group` | TEXT | Yes | No | — | — |
| `reason` | TEXT | No | Yes | — | — |

Indexes: `idx_measure_break_starts_group` (metric_group, partial unique), `idx_measure_breaks_identity` (period, kind, expression, unique), `sqlite_autoindex_measure_breaks_1` (period, kind, metric_group, unique).

### `measure_definitions`

Source: `backend/infra/outcome_events_schema.py`.

| Column | Type | Primary key | NOT NULL | Default | References |
|---|---|---|---|---|---|
| `owner_thread_id` | TEXT | Yes | Yes | — | — |
| `kind` | TEXT | Yes | Yes | — | — |
| `name` | TEXT | Yes | Yes | — | — |
| `version` | INTEGER | Yes | Yes | — | — |
| `spec_json` | TEXT | No | Yes | — | — |
| `file_path` | TEXT | No | Yes | — | — |
| `file_sha256` | TEXT | No | Yes | — | — |
| `synced_at` | TEXT | No | Yes | — | — |
| `removed` | INTEGER | No | Yes | 0 | — |
| `version_attr` | TEXT | No | No | — | — |

Indexes: `idx_measure_definitions_kind_name` (kind, name), `sqlite_autoindex_measure_definitions_1` (owner_thread_id, kind, name, version, unique).

### `measure_keys`

Source: `backend/infra/measure_keys_schema.py`.

| Column | Type | Primary key | NOT NULL | Default | References |
|---|---|---|---|---|---|
| `key_id` | TEXT | Yes | No | — | — |
| `key_hash` | TEXT | No | Yes | — | — |
| `agent_thread_id` | TEXT | No | Yes | — | — |
| `label` | TEXT | No | Yes | — | — |
| `created_at` | TEXT | No | Yes | — | — |
| `revoked_at` | TEXT | No | No | — | — |

Indexes: `idx_measure_keys_agent` (agent_thread_id, created_at), `sqlite_autoindex_measure_keys_1` (key_id, unique), `sqlite_autoindex_measure_keys_2` (key_hash, unique).

### `measurement_outcome_nodes`

Source: `backend/api/measurement_prepared.py`.

| Column | Type | Primary key | NOT NULL | Default | References |
|---|---|---|---|---|---|
| `node_id` | TEXT | Yes | No | — | — |
| `root_id` | TEXT | No | Yes | — | — |
| `period` | TEXT | No | Yes | — | — |
| `first_try` | INTEGER | No | Yes | — | — |
| `difficulty` | TEXT | No | No | — | — |

Indexes: `sqlite_autoindex_measurement_outcome_nodes_1` (node_id, unique).

### `measurement_outcome_periods`

Source: `backend/api/measurement_prepared.py`.

| Column | Type | Primary key | NOT NULL | Default | References |
|---|---|---|---|---|---|
| `period` | TEXT | Yes | No | — | — |
| `outcomes` | INTEGER | No | Yes | — | — |
| `first_try` | INTEGER | No | Yes | — | — |
| `rejections` | INTEGER | No | Yes | — | — |

Indexes: `sqlite_autoindex_measurement_outcome_periods_1` (period, unique).

### `measurement_root_difficulty_periods`

Source: `backend/api/measurement_prepared.py`.

| Column | Type | Primary key | NOT NULL | Default | References |
|---|---|---|---|---|---|
| `root_id` | TEXT | Yes | Yes | — | — |
| `period` | TEXT | Yes | Yes | — | — |
| `difficulty` | TEXT | Yes | Yes | — | — |
| `count` | INTEGER | No | Yes | — | — |

Indexes: `sqlite_autoindex_measurement_root_difficulty_periods_1` (root_id, period, difficulty, unique).

### `measurement_root_outcome_periods`

Source: `backend/api/measurement_prepared.py`.

| Column | Type | Primary key | NOT NULL | Default | References |
|---|---|---|---|---|---|
| `root_id` | TEXT | Yes | Yes | — | — |
| `period` | TEXT | Yes | Yes | — | — |
| `outcomes` | INTEGER | No | Yes | — | — |
| `first_try` | INTEGER | No | Yes | — | — |

Indexes: `sqlite_autoindex_measurement_root_outcome_periods_1` (root_id, period, unique).

### `measurement_scope_roots`

Source: `backend/api/measurement_prepared.py`.

| Column | Type | Primary key | NOT NULL | Default | References |
|---|---|---|---|---|---|
| `id` | TEXT | Yes | No | — | — |

Indexes: `sqlite_autoindex_measurement_scope_roots_1` (id, unique).

### `measurement_scope_threads`

Source: `backend/api/measurement_prepared.py`.

| Column | Type | Primary key | NOT NULL | Default | References |
|---|---|---|---|---|---|
| `id` | TEXT | Yes | No | — | — |
| `root_id` | TEXT | No | No | — | — |

Indexes: `sqlite_autoindex_measurement_scope_threads_1` (id, unique).

### `measurement_task_chain`

Source: `backend/api/measurement_prepared.py`.

| Column | Type | Primary key | NOT NULL | Default | References |
|---|---|---|---|---|---|
| `task_id` | TEXT | No | No | — | — |
| `period` | TEXT | No | No | — | — |
| `settled_at` | TEXT | No | No | — | — |
| `node_id` | TEXT | No | No | — | — |

Indexes: None.

### `model_pricing`

Source: `backend/infra/usage_schema.py`.

| Column | Type | Primary key | NOT NULL | Default | References |
|---|---|---|---|---|---|
| `id` | TEXT | Yes | No | — | — |
| `model` | TEXT | No | Yes | — | — |
| `effective_date` | TEXT | No | Yes | — | — |
| `input_cost_per_token` | REAL | No | Yes | 0.0 | — |
| `output_cost_per_token` | REAL | No | Yes | 0.0 | — |
| `cache_creation_cost_per_token` | REAL | No | Yes | 0.0 | — |
| `cache_read_cost_per_token` | REAL | No | Yes | 0.0 | — |
| `source` | TEXT | No | Yes | 'litellm_sync' | — |
| `created_at` | TEXT | No | Yes | — | — |

Indexes: `idx_model_pricing_lookup` (model, effective_date), `sqlite_autoindex_model_pricing_1` (id, unique).

### `outcome_event_counters`

Source: `backend/infra/outcome_events_schema.py`.

| Column | Type | Primary key | NOT NULL | Default | References |
|---|---|---|---|---|---|
| `counter` | TEXT | Yes | Yes | — | — |
| `event_name` | TEXT | Yes | Yes | — | — |
| `agent_thread_id` | TEXT | Yes | Yes | '' | — |
| `day` | TEXT | Yes | Yes | '0000-00-00' | — |
| `n` | INTEGER | No | Yes | 0 | — |

Indexes: `sqlite_autoindex_outcome_event_counters_1` (agent_thread_id, day, event_name, counter, unique).

### `outcome_events`

Source: `backend/infra/outcome_events_schema.py`.

| Column | Type | Primary key | NOT NULL | Default | References |
|---|---|---|---|---|---|
| `id` | TEXT | Yes | No | — | — |
| `dedupe_key` | TEXT | No | Yes | — | — |
| `event_name` | TEXT | No | Yes | — | — |
| `source` | TEXT | No | Yes | — | — |
| `provenance` | TEXT | No | Yes | — | — |
| `ts` | TEXT | No | Yes | — | — |
| `observed_ts` | TEXT | No | Yes | — | — |
| `subject_key` | TEXT | No | No | — | — |
| `project_id` | TEXT | No | No | — | — |
| `agent_thread_id` | TEXT | No | No | — | — |
| `attribution` | TEXT | No | No | — | — |
| `corroborated_by` | TEXT | No | No | — | — |
| `attributes` | TEXT | No | Yes | — | — |
| `retracted_at` | TEXT | No | No | — | — |

Indexes: `idx_learning_loop_outcome_subject` (subject_key, partial unique), `idx_learning_loop_triage_subject` (subject_key, partial unique), `idx_measurement_size_rater` (expression, subject_key), `idx_outcome_events_agent_observed` (agent_thread_id, observed_ts, event_name), `idx_outcome_events_agent_ts` (agent_thread_id, ts), `idx_outcome_events_name_ts` (event_name, ts), `idx_outcome_events_project_ts` (project_id, ts), `idx_outcome_events_subject` (subject_key), `sqlite_autoindex_outcome_events_1` (id, unique), `sqlite_autoindex_outcome_events_2` (dedupe_key, unique).
