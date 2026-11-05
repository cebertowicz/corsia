# Events and webhooks

These tables store events, rules, watches, bulletins and webhook deliveries.

## Tables

### `dead_letter_events`

Source: `backend/infra/schema.py`.

| Column | Type | Primary key | NOT NULL | Default | References |
|---|---|---|---|---|---|
| `id` | TEXT | Yes | No | — | — |
| `internal_event` | TEXT | No | Yes | — | — |
| `external_id` | TEXT | No | No | — | — |
| `reason` | TEXT | No | Yes | — | — |
| `payload_json` | TEXT | No | No | — | — |
| `status` | TEXT | No | Yes | 'parked' | — |
| `created_at` | TEXT | No | Yes | — | — |

Indexes: `idx_dead_letter_status` (status, created_at), `sqlite_autoindex_dead_letter_events_1` (id, unique).

### `event_intake_records`

Source: `backend/infra/event_intake_schema.py`.

| Column | Type | Primary key | NOT NULL | Default | References |
|---|---|---|---|---|---|
| `id` | INTEGER | Yes | No | — | — |
| `event_id` | TEXT | No | Yes | — | — |
| `outcome` | TEXT | No | Yes | — | — |
| `rules_matched` | INTEGER | No | Yes | — | — |
| `refusal_code` | TEXT | No | No | — | — |
| `producer` | TEXT | No | No | — | — |
| `origin` | TEXT | No | No | — | — |
| `event_type` | TEXT | No | No | — | — |
| `at` | TEXT | No | Yes | — | — |

Indexes: `idx_event_intake_accepted` (event_id, partial unique), `idx_event_intake_type` (outcome, event_type, at).

### `event_route_decisions`

Source: `backend/infra/event_rule_schema.py`.

| Column | Type | Primary key | NOT NULL | Default | References |
|---|---|---|---|---|---|
| `id` | INTEGER | Yes | No | — | — |
| `at` | TEXT | No | Yes | — | — |
| `event_id` | TEXT | No | Yes | — | — |
| `connector` | TEXT | No | Yes | — | — |
| `event_type` | TEXT | No | Yes | — | — |
| `watch_event` | TEXT | No | Yes | — | — |
| `artifact_integration` | TEXT | No | No | — | — |
| `artifact_type` | TEXT | No | No | — | — |
| `artifact_external_id` | TEXT | No | No | — | — |
| `rule_id` | TEXT | No | Yes | — | — |
| `thread_id` | TEXT | No | No | — | — |
| `outcome` | TEXT | No | Yes | — | — |
| `via` | TEXT | No | Yes | — | — |
| `reason` | TEXT | No | No | — | — |
| `run_input` | TEXT | No | No | — | — |
| `request_identity` | TEXT | No | No | — | — |
| `worker_thread_id` | TEXT | No | No | — | — |

Indexes: `idx_event_route_decisions_artifact` (artifact_external_id, at), `idx_event_route_decisions_at` (at), `idx_event_route_decisions_event` (event_id), `idx_event_route_decisions_request` (rule_id, request_identity), `idx_event_route_decisions_rule` (rule_id, thread_id, outcome, at), `idx_event_route_decisions_runs` (outcome, id), `idx_event_route_decisions_worker` (worker_thread_id).

### `event_rules`

Source: `backend/infra/event_rule_schema.py`.

| Column | Type | Primary key | NOT NULL | Default | References |
|---|---|---|---|---|---|
| `id` | TEXT | Yes | Yes | — | — |
| `created_at` | TEXT | No | Yes | — | — |
| `updated_at` | TEXT | No | Yes | — | — |
| `enabled` | INTEGER | No | Yes | 1 | — |
| `kind` | TEXT | No | Yes | 'route' | — |
| `scope` | TEXT | No | Yes | — | — |
| `connector` | TEXT | No | Yes | — | — |
| `event_type` | TEXT | No | No | — | — |
| `match` | TEXT | No | Yes | '[]' | — |
| `action` | TEXT | No | No | — | — |
| `to` | TEXT | No | No | — | — |
| `mode` | TEXT | No | No | — | — |
| `note` | TEXT | No | No | — | — |
| `origin` | TEXT | No | Yes | — | — |
| `created_by` | TEXT | No | Yes | — | — |
| `authority_ref` | TEXT | No | Yes | — | — |
| `script_id` | TEXT | No | No | — | — |
| `workflow_id` | TEXT | No | No | — | — |
| `request_identity_fact` | TEXT | No | No | — | — |
| `tool` | TEXT | No | No | — | — |
| `argv_prefix` | TEXT | No | No | — | — |
| `item_type` | TEXT | No | No | — | — |
| `worker_profile` | TEXT | No | No | — | — |

Indexes: `idx_event_rules_observe` (tool, enabled, scope), `idx_event_rules_scope` (scope, created_at), `idx_event_rules_type` (connector, event_type, enabled, scope, created_at), `sqlite_autoindex_event_rules_1` (id, unique).

### `expected_event_watches`

Source: `backend/infra/event_watch_schema.py`.

| Column | Type | Primary key | NOT NULL | Default | References |
|---|---|---|---|---|---|
| `id` | TEXT | Yes | No | — | — |
| `artifact_integration` | TEXT | No | No | — | — |
| `artifact_type` | TEXT | No | No | — | — |
| `artifact_external_id` | TEXT | No | No | — | — |
| `owner_thread_id` | TEXT | No | No | — | — |
| `plan_id` | TEXT | No | No | — | plans.id |
| `plan_node_id` | TEXT | No | No | — | plan_nodes.id |
| `armed_by_actor_id` | TEXT | No | No | — | — |
| `expected_event` | TEXT | No | No | — | — |
| `condition_type` | TEXT | No | Yes | — | — |
| `condition_value` | TEXT | No | No | — | — |
| `status` | TEXT | No | Yes | 'armed' | — |
| `armed_at` | TEXT | No | Yes | — | — |
| `resolved_at` | TEXT | No | No | — | — |
| `expired_at` | TEXT | No | No | — | — |
| `routed_at` | TEXT | No | No | — | — |
| `created_at` | TEXT | No | Yes | — | — |
| `updated_at` | TEXT | No | Yes | — | — |

Indexes: `idx_watches_dedupe` (artifact_integration, artifact_type, artifact_external_id, owner_thread_id, condition_type, condition_value, expected_event, partial unique), `idx_watches_status` (status, condition_type), `sqlite_autoindex_expected_event_watches_1` (id, unique).

### `self_caused_events`

Source: `backend/infra/event_rule_schema.py`.

| Column | Type | Primary key | NOT NULL | Default | References |
|---|---|---|---|---|---|
| `connector` | TEXT | Yes | Yes | — | — |
| `event_key` | TEXT | Yes | Yes | — | — |
| `thread_id` | TEXT | Yes | Yes | — | — |
| `at` | TEXT | No | Yes | — | — |

Indexes: `idx_self_caused_events_at` (at), `sqlite_autoindex_self_caused_events_1` (connector, event_key, thread_id, unique).

### `system_bulletin_identity`

Source: `backend/infra/bulletin_schema.py`.

| Column | Type | Primary key | NOT NULL | Default | References |
|---|---|---|---|---|---|
| `id` | INTEGER | Yes | No | — | — |
| `version` | TEXT | No | Yes | — | — |
| `digest` | TEXT | No | Yes | — | — |
| `files_json` | TEXT | No | Yes | — | — |
| `reconciled_at` | TEXT | No | No | — | — |

Indexes: None.

### `system_bulletins`

Source: `backend/infra/bulletin_schema.py`.

| Column | Type | Primary key | NOT NULL | Default | References |
|---|---|---|---|---|---|
| `id` | TEXT | Yes | No | — | — |
| `source` | TEXT | No | Yes | — | — |
| `state` | TEXT | No | Yes | — | — |
| `disposition` | TEXT | No | No | — | — |
| `delivery_id` | TEXT | No | No | — | — |
| `delivered_at` | TEXT | No | No | — | — |
| `acted_at` | TEXT | No | No | — | — |
| `withdrawn_at` | TEXT | No | No | — | — |
| `updated_at` | TEXT | No | Yes | — | — |

Indexes: `sqlite_autoindex_system_bulletins_1` (id, unique).

### `webhook_deliveries`

Source: `backend/infra/webhook_delivery_schema.py`.

| Column | Type | Primary key | NOT NULL | Default | References |
|---|---|---|---|---|---|
| `id` | TEXT | Yes | No | — | — |
| `event_type` | TEXT | No | Yes | — | — |
| `action` | TEXT | No | No | — | — |
| `delivery_id` | TEXT | No | Yes | — | — |
| `repo` | TEXT | No | No | — | — |
| `payload_hash` | TEXT | No | Yes | — | — |
| `payload_json` | TEXT | No | No | — | — |
| `status` | TEXT | No | Yes | 'received' | — |
| `error` | TEXT | No | No | — | — |
| `created_at` | TEXT | No | Yes | — | — |
| `object_key` | TEXT | No | No | — | — |
| `check_suite_id` | INTEGER | No | No | — | — |

Indexes: `idx_webhook_deliveries_check_suite` (check_suite_id, created_at), `idx_webhook_deliveries_event` (event_type, created_at), `idx_webhook_deliveries_object_key` (object_key, partial unique), `idx_webhook_deliveries_status` (status, created_at), `sqlite_autoindex_webhook_deliveries_1` (id, unique), `sqlite_autoindex_webhook_deliveries_2` (delivery_id, unique).
