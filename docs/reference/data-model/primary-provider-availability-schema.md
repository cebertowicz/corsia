# Provider Availability Schema

Tables in the primary store created by `backend/infra/provider_availability_schema.py`.

## Tables 1–1

```mermaid
erDiagram
    provider_availability {
        TEXT harness PK
        TEXT provider PK
        TEXT account_scope PK
        TEXT status
        TEXT observed_at
        TEXT reset_at
        TEXT source
        INTEGER evidence_rank
        TEXT evidence_json
        TEXT expires_at
        TEXT updated_at
    }
```

### `provider_availability`

| Column | Type | Primary key | NOT NULL | Default | References ||---|---|---|---|---|---|
| `harness` | TEXT | Yes | Yes | — | — |
| `provider` | TEXT | Yes | Yes | — | — |
| `account_scope` | TEXT | Yes | Yes | — | — |
| `status` | TEXT | No | Yes | — | — |
| `observed_at` | TEXT | No | Yes | — | — |
| `reset_at` | TEXT | No | No | — | — |
| `source` | TEXT | No | Yes | — | — |
| `evidence_rank` | INTEGER | No | Yes | — | — |
| `evidence_json` | TEXT | No | Yes | — | — |
| `expires_at` | TEXT | No | Yes | — | — |
| `updated_at` | TEXT | No | Yes | — | — |

Indexes: `sqlite_autoindex_provider_availability_1` (harness, provider, account_scope, unique).
