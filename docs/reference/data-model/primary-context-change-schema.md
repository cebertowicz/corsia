# Context Change Schema

Tables in the primary store created by `backend/infra/context_change_schema.py`.

## Tables 1–1

```mermaid
erDiagram
    context_change_journal {
        INTEGER seq PK
        TEXT object_kind
        TEXT object_id
        TEXT change_kind
        TEXT summary
        TEXT details_json
        TEXT actor_thread_id
        TEXT created_at
    }
```

### `context_change_journal`

| Column | Type | Primary key | NOT NULL | Default | References ||---|---|---|---|---|---|
| `seq` | INTEGER | Yes | No | — | — |
| `object_kind` | TEXT | No | Yes | — | — |
| `object_id` | TEXT | No | Yes | — | — |
| `change_kind` | TEXT | No | Yes | — | — |
| `summary` | TEXT | No | Yes | — | — |
| `details_json` | TEXT | No | No | — | — |
| `actor_thread_id` | TEXT | No | No | — | — |
| `created_at` | TEXT | No | Yes | — | — |

Indexes: `idx_context_changes_object` (object_kind, object_id, seq).
