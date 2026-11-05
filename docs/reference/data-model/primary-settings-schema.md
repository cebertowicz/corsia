# Settings Schema

Tables in the primary store created by `backend/infra/settings_schema.py`.

## Tables 1–1

```mermaid
erDiagram
    settings {
        TEXT key PK
        TEXT value
        TEXT updated_at
    }
```

### `settings`

| Column | Type | Primary key | NOT NULL | Default | References ||---|---|---|---|---|---|
| `key` | TEXT | Yes | No | — | — |
| `value` | TEXT | No | Yes | — | — |
| `updated_at` | TEXT | No | Yes | — | — |

Indexes: `sqlite_autoindex_settings_1` (key, unique).
