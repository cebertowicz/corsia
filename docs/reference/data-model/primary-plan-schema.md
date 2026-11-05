# Plan Schema

Tables in the primary store created by `backend/infra/plan_schema.py`.

## Tables 1–4

```mermaid
erDiagram
    plan_ask_deferrals {
        TEXT hold_id PK
        TEXT deferred_at
    }
    plan_mutation_receipts {
        TEXT plan_id PK
        TEXT mutation_id PK
        TEXT actor_id
        TEXT operation
        TEXT request_fingerprint
        TEXT result_json
        TEXT created_at
    }
    plan_node_artifact_links {
        TEXT node_id PK
        TEXT artifact_id PK
        TEXT role PK
        TEXT actor_id
        TEXT provenance
        TEXT source_event_id
        TEXT created_at
    }
    plan_node_attempts {
        TEXT id PK
        TEXT plan_id
        TEXT node_id
        TEXT executor_thread_id
        INTEGER requirements_revision
        INTEGER attempt_ordinal
        TEXT selected_by_actor_id
        TEXT provenance
        TEXT selected_at
        TEXT released_at
        TEXT release_reason
    }
```

### `plan_ask_deferrals`

| Column | Type | Primary key | NOT NULL | Default | References ||---|---|---|---|---|---|
| `hold_id` | TEXT | Yes | No | — | plan_node_holds.id |
| `deferred_at` | TEXT | No | Yes | — | — |

Indexes: `sqlite_autoindex_plan_ask_deferrals_1` (hold_id, unique).

### `plan_mutation_receipts`

| Column | Type | Primary key | NOT NULL | Default | References ||---|---|---|---|---|---|
| `plan_id` | TEXT | Yes | Yes | — | plans.id |
| `mutation_id` | TEXT | Yes | Yes | — | — |
| `actor_id` | TEXT | No | Yes | — | — |
| `operation` | TEXT | No | Yes | — | — |
| `request_fingerprint` | TEXT | No | Yes | — | — |
| `result_json` | TEXT | No | Yes | — | — |
| `created_at` | TEXT | No | Yes | — | — |

Indexes: `sqlite_autoindex_plan_mutation_receipts_1` (plan_id, mutation_id, unique).

### `plan_node_artifact_links`

| Column | Type | Primary key | NOT NULL | Default | References ||---|---|---|---|---|---|
| `node_id` | TEXT | Yes | Yes | — | plan_nodes.id |
| `artifact_id` | TEXT | Yes | Yes | — | artifact_identities.id |
| `role` | TEXT | Yes | Yes | — | — |
| `actor_id` | TEXT | No | No | — | — |
| `provenance` | TEXT | No | Yes | — | — |
| `source_event_id` | TEXT | No | No | — | — |
| `created_at` | TEXT | No | Yes | — | — |

Indexes: `idx_plan_node_artifact_links_artifact` (artifact_id, node_id), `sqlite_autoindex_plan_node_artifact_links_1` (node_id, artifact_id, role, unique).

### `plan_node_attempts`

| Column | Type | Primary key | NOT NULL | Default | References ||---|---|---|---|---|---|
| `id` | TEXT | Yes | No | — | — |
| `plan_id` | TEXT | No | Yes | — | plans.id |
| `node_id` | TEXT | No | Yes | — | plan_nodes.id |
| `executor_thread_id` | TEXT | No | Yes | — | threads.id |
| `requirements_revision` | INTEGER | No | Yes | — | — |
| `attempt_ordinal` | INTEGER | No | Yes | — | — |
| `selected_by_actor_id` | TEXT | No | No | — | — |
| `provenance` | TEXT | No | Yes | — | — |
| `selected_at` | TEXT | No | Yes | — | — |
| `released_at` | TEXT | No | No | — | — |
| `release_reason` | TEXT | No | No | — | — |

Indexes: `idx_plan_attempt_commitment` (node_id, selected_at), `idx_plan_attempt_open_executor` (plan_id, executor_thread_id, unique), `idx_plan_attempt_open_node` (node_id, unique), `idx_plan_node_attempts_released` (released_at), `idx_plan_node_attempts_selected` (selected_at), `sqlite_autoindex_plan_node_attempts_1` (id, unique), `sqlite_autoindex_plan_node_attempts_2` (node_id, attempt_ordinal, unique).

## Tables 5–8

```mermaid
erDiagram
    plan_node_dependencies {
        TEXT node_id PK
        TEXT depends_on_node_id PK
        TEXT created_at
    }
    plan_node_events {
        TEXT id PK
        TEXT node_id
        TEXT actor_id
        TEXT provenance
        TEXT event
        TEXT prior_state
        TEXT new_state
        TEXT verification_result
        TEXT causal_event_id
        TEXT payload_json
        TEXT created_at
    }
    plan_node_holds {
        TEXT id PK
        TEXT node_id
        TEXT kind
        TEXT reason_key
        TEXT reason
        TEXT owner_kind
        TEXT owner_id
        TEXT created_by_actor_id
        TEXT provenance
        TEXT created_at
        TEXT cleared_by_actor_id
        TEXT cleared_at
        TEXT clearance_event_id
    }
    plan_nodes {
        TEXT id PK
        TEXT plan_id
        TEXT parent_id
        INTEGER order_key
        TEXT kind
        TEXT title
        TEXT description
        TEXT intent
        TEXT intent_node_id
        TEXT workflow_ref
        TEXT workflow_version
        TEXT workflow_step
        TEXT difficulty
        TEXT size
        TEXT acceptance_criteria
        INTEGER checkpoint_exception
        TEXT state
        TEXT assignee
        TEXT spawn_kind
        TEXT owner_thread_id
        TEXT executor_thread_id
        TEXT spawned_thread_id
        TEXT follow_up_of
        INTEGER requirements_revision
        TEXT commitment_id
        INTEGER watchdog_budget_seconds
        INTEGER revision
        TEXT created_at
        TEXT updated_at
        TEXT archived_at
        INTEGER sent_back
        TEXT own_last_changed
        INTEGER listed
        INTEGER child_in_work
        INTEGER own_in_work
        INTEGER sub_open
        INTEGER sub_done
        INTEGER sub_failed
        INTEGER sub_closed
        INTEGER sub_in_work
        INTEGER prop_open
        INTEGER prop_done
        INTEGER prop_failed
        INTEGER prop_closed
        INTEGER prop_in_work
        INTEGER folded
        TEXT folded_at
        TEXT fold_bucket
        INTEGER child_folded_done
        INTEGER child_folded_failed
        INTEGER child_folded_closed
    }
    plan_nodes ||--o{ plan_node_dependencies : references
    plan_nodes ||--o{ plan_node_dependencies : references
    plan_node_events o|--o{ plan_node_events : references
    plan_nodes ||--o{ plan_node_events : references
    plan_node_events o|--o{ plan_node_holds : references
    plan_nodes ||--o| plan_node_holds : references
    plan_nodes ||--o{ plan_nodes : references
    plan_nodes o|--o{ plan_nodes : references
    plan_nodes o|--o{ plan_nodes : references
    plan_nodes o|--o{ plan_nodes : references
```

### `plan_node_dependencies`

| Column | Type | Primary key | NOT NULL | Default | References ||---|---|---|---|---|---|
| `node_id` | TEXT | Yes | Yes | — | plan_nodes.id |
| `depends_on_node_id` | TEXT | Yes | Yes | — | plan_nodes.id |
| `created_at` | TEXT | No | Yes | — | — |

Indexes: `idx_plan_node_dependencies_target` (depends_on_node_id), `sqlite_autoindex_plan_node_dependencies_1` (node_id, depends_on_node_id, unique).

### `plan_node_events`

| Column | Type | Primary key | NOT NULL | Default | References ||---|---|---|---|---|---|
| `id` | TEXT | Yes | No | — | — |
| `node_id` | TEXT | No | Yes | — | plan_nodes.id |
| `actor_id` | TEXT | No | No | — | — |
| `provenance` | TEXT | No | Yes | — | — |
| `event` | TEXT | No | Yes | — | — |
| `prior_state` | TEXT | No | No | — | — |
| `new_state` | TEXT | No | No | — | — |
| `verification_result` | TEXT | No | No | — | — |
| `causal_event_id` | TEXT | No | No | — | plan_node_events.id |
| `payload_json` | TEXT | No | No | — | — |
| `created_at` | TEXT | No | Yes | — | — |

Indexes: `idx_measurement_rejections_time` (created_at, node_id), `idx_plan_node_events_created` (created_at), `idx_plan_node_events_node` (node_id, created_at), `sqlite_autoindex_plan_node_events_1` (id, unique).

### `plan_node_holds`

| Column | Type | Primary key | NOT NULL | Default | References ||---|---|---|---|---|---|
| `id` | TEXT | Yes | No | — | — |
| `node_id` | TEXT | No | Yes | — | plan_nodes.id |
| `kind` | TEXT | No | Yes | — | — |
| `reason_key` | TEXT | No | Yes | — | — |
| `reason` | TEXT | No | Yes | — | — |
| `owner_kind` | TEXT | No | Yes | — | — |
| `owner_id` | TEXT | No | No | — | — |
| `created_by_actor_id` | TEXT | No | No | — | — |
| `provenance` | TEXT | No | Yes | — | — |
| `created_at` | TEXT | No | Yes | — | — |
| `cleared_by_actor_id` | TEXT | No | No | — | — |
| `cleared_at` | TEXT | No | No | — | — |
| `clearance_event_id` | TEXT | No | No | — | plan_node_events.id |

Indexes: `idx_plan_holds_active_node` (node_id, kind), `idx_plan_holds_one_pause` (node_id, unique), `sqlite_autoindex_plan_node_holds_1` (id, unique).

### `plan_nodes`

| Column | Type | Primary key | NOT NULL | Default | References ||---|---|---|---|---|---|
| `id` | TEXT | Yes | No | — | — |
| `plan_id` | TEXT | No | Yes | — | plans.id |
| `parent_id` | TEXT | No | No | — | plan_nodes.id |
| `order_key` | INTEGER | No | Yes | — | — |
| `kind` | TEXT | No | Yes | — | — |
| `title` | TEXT | No | Yes | — | — |
| `description` | TEXT | No | No | — | — |
| `intent` | TEXT | No | No | — | — |
| `intent_node_id` | TEXT | No | No | — | plan_nodes.id |
| `workflow_ref` | TEXT | No | No | — | — |
| `workflow_version` | TEXT | No | No | — | — |
| `workflow_step` | TEXT | No | No | — | — |
| `difficulty` | TEXT | No | No | — | — |
| `size` | TEXT | No | No | — | — |
| `acceptance_criteria` | TEXT | No | Yes | — | — |
| `checkpoint_exception` | INTEGER | No | Yes | 0 | — |
| `state` | TEXT | No | Yes | 'pending' | — |
| `assignee` | TEXT | No | No | — | — |
| `spawn_kind` | TEXT | No | No | — | — |
| `owner_thread_id` | TEXT | No | No | — | threads.id |
| `executor_thread_id` | TEXT | No | No | — | threads.id |
| `spawned_thread_id` | TEXT | No | No | — | threads.id |
| `follow_up_of` | TEXT | No | No | — | plan_nodes.id |
| `requirements_revision` | INTEGER | No | Yes | 1 | — |
| `commitment_id` | TEXT | No | Yes | — | plan_nodes.id |
| `watchdog_budget_seconds` | INTEGER | No | No | — | — |
| `revision` | INTEGER | No | Yes | 1 | — |
| `created_at` | TEXT | No | Yes | — | — |
| `updated_at` | TEXT | No | Yes | — | — |
| `archived_at` | TEXT | No | No | — | — |
| `sent_back` | INTEGER | No | Yes | 0 | — |
| `own_last_changed` | TEXT | No | No | — | — |
| `listed` | INTEGER | No | Yes | 0 | — |
| `child_in_work` | INTEGER | No | Yes | 0 | — |
| `own_in_work` | INTEGER | No | Yes | 0 | — |
| `sub_open` | INTEGER | No | Yes | 0 | — |
| `sub_done` | INTEGER | No | Yes | 0 | — |
| `sub_failed` | INTEGER | No | Yes | 0 | — |
| `sub_closed` | INTEGER | No | Yes | 0 | — |
| `sub_in_work` | INTEGER | No | Yes | 0 | — |
| `prop_open` | INTEGER | No | Yes | 0 | — |
| `prop_done` | INTEGER | No | Yes | 0 | — |
| `prop_failed` | INTEGER | No | Yes | 0 | — |
| `prop_closed` | INTEGER | No | Yes | 0 | — |
| `prop_in_work` | INTEGER | No | Yes | 0 | — |
| `folded` | INTEGER | No | Yes | 0 | — |
| `folded_at` | TEXT | No | No | — | — |
| `fold_bucket` | TEXT | No | No | — | — |
| `child_folded_done` | INTEGER | No | Yes | 0 | — |
| `child_folded_failed` | INTEGER | No | Yes | 0 | — |
| `child_folded_closed` | INTEGER | No | Yes | 0 | — |

Indexes: `idx_plan_nodes_commitment` (commitment_id, created_at), `idx_plan_nodes_executor_thread` (executor_thread_id), `idx_plan_nodes_folded` (plan_id, parent_id, folded_at, id), `idx_plan_nodes_follow_up` (follow_up_of), `idx_plan_nodes_intent_node` (intent_node_id), `idx_plan_nodes_last_changed` (parent_id, own_last_changed), `idx_plan_nodes_open` (plan_id, parent_id, order_key), `idx_plan_nodes_recent` (plan_id, own_last_changed), `idx_plan_nodes_spawned_thread` (spawned_thread_id), `idx_plan_nodes_stale` (plan_id), `idx_plan_nodes_tree` (plan_id, parent_id, order_key), `idx_plan_nodes_updated` (updated_at), `idx_plan_nodes_user_failed` (plan_id, own_last_changed), `idx_plan_nodes_workflow_ref` (workflow_ref), `sqlite_autoindex_plan_nodes_1` (id, unique).

## Tables 9–10

```mermaid
erDiagram
    plan_tool_dispatches {
        TEXT id PK
        TEXT attempt_id
        TEXT turn_id
        TEXT tool_name
        TEXT started_at
        TEXT settled_at
        TEXT outcome
        TEXT result_message_id
    }
    plans {
        TEXT id PK
        TEXT owner_kind
        TEXT owner_id
        TEXT parent_plan_id
        TEXT state
        INTEGER revision
        TEXT created_at
        TEXT updated_at
        TEXT archived_at
        INTEGER top_open
        INTEGER top_done
        INTEGER top_failed
        INTEGER top_closed
        INTEGER top_in_work
        INTEGER top_roots
        INTEGER top_folded_done
        INTEGER top_folded_failed
        INTEGER top_folded_closed
        INTEGER settled_events
    }
    plans o|--o{ plans : references
```

### `plan_tool_dispatches`

| Column | Type | Primary key | NOT NULL | Default | References ||---|---|---|---|---|---|
| `id` | TEXT | Yes | No | — | — |
| `attempt_id` | TEXT | No | Yes | — | plan_node_attempts.id |
| `turn_id` | TEXT | No | Yes | — | — |
| `tool_name` | TEXT | No | Yes | — | — |
| `started_at` | TEXT | No | Yes | — | — |
| `settled_at` | TEXT | No | No | — | — |
| `outcome` | TEXT | No | No | — | — |
| `result_message_id` | TEXT | No | No | — | — |

Indexes: `idx_plan_tool_dispatch_open_attempt` (attempt_id), `idx_plan_tool_dispatch_turn` (turn_id, started_at), `sqlite_autoindex_plan_tool_dispatches_1` (id, unique).

### `plans`

| Column | Type | Primary key | NOT NULL | Default | References ||---|---|---|---|---|---|
| `id` | TEXT | Yes | No | — | — |
| `owner_kind` | TEXT | No | Yes | — | — |
| `owner_id` | TEXT | No | Yes | — | — |
| `parent_plan_id` | TEXT | No | No | — | plans.id |
| `state` | TEXT | No | Yes | 'active' | — |
| `revision` | INTEGER | No | Yes | 1 | — |
| `created_at` | TEXT | No | Yes | — | — |
| `updated_at` | TEXT | No | Yes | — | — |
| `archived_at` | TEXT | No | No | — | — |
| `top_open` | INTEGER | No | Yes | 0 | — |
| `top_done` | INTEGER | No | Yes | 0 | — |
| `top_failed` | INTEGER | No | Yes | 0 | — |
| `top_closed` | INTEGER | No | Yes | 0 | — |
| `top_in_work` | INTEGER | No | Yes | 0 | — |
| `top_roots` | INTEGER | No | Yes | 0 | — |
| `top_folded_done` | INTEGER | No | Yes | 0 | — |
| `top_folded_failed` | INTEGER | No | Yes | 0 | — |
| `top_folded_closed` | INTEGER | No | Yes | 0 | — |
| `settled_events` | INTEGER | No | Yes | 0 | — |

Indexes: `idx_plans_parent` (parent_plan_id), `sqlite_autoindex_plans_1` (id, unique), `sqlite_autoindex_plans_2` (owner_kind, owner_id, unique).
