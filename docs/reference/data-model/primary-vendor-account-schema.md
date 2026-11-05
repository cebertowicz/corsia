# Vendor Account Schema

Tables in the primary store created by `backend/infra/vendor_account_schema.py`.

## Tables 1–4

```mermaid
erDiagram
    thread_vendor_accounts {
        TEXT thread_id PK
        TEXT provider PK
        TEXT account_id
        TEXT pending_account_id
        TEXT deferred_move
    }
    vendor_account_billing_facts {
        TEXT id PK
        TEXT account_id
        TEXT network_server_id
        TEXT kind
        INTEGER amount_cents
        TEXT start_date
        TEXT end_date
        INTEGER charge_day
        TEXT provenance
        INTEGER account_revision
        INTEGER receipt_revision
        INTEGER receipt_login_seq
        TEXT supersedes_id
        TEXT created_at
    }
    vendor_account_billing_revisions {
        TEXT account_id
        TEXT network_server_id
        INTEGER revision
    }
    vendor_accounts {
        TEXT id PK
        TEXT connection_id
        TEXT name
        TEXT source_home
        TEXT runtime_home
        INTEGER is_default
        TEXT state
        INTEGER revision
        TEXT identity_json
        TEXT auth_cleanup_json
        TEXT discovery_json
        TEXT usage_json
        TEXT created_at
        TEXT updated_at
        TEXT removed_at
    }
    vendor_accounts o|--o{ thread_vendor_accounts : references
    vendor_accounts ||--o{ thread_vendor_accounts : references
    vendor_account_billing_facts o|--o| vendor_account_billing_facts : references
    vendor_accounts o|--o{ vendor_account_billing_facts : references
    vendor_accounts o|--o| vendor_account_billing_revisions : references
```

### `thread_vendor_accounts`

| Column | Type | Primary key | NOT NULL | Default | References ||---|---|---|---|---|---|
| `thread_id` | TEXT | Yes | Yes | — | threads.id |
| `provider` | TEXT | Yes | Yes | — | — |
| `account_id` | TEXT | No | Yes | — | vendor_accounts.id |
| `pending_account_id` | TEXT | No | No | — | vendor_accounts.id |
| `deferred_move` | TEXT | No | No | — | — |

Indexes: `sqlite_autoindex_thread_vendor_accounts_1` (thread_id, provider, unique).

### `vendor_account_billing_facts`

| Column | Type | Primary key | NOT NULL | Default | References ||---|---|---|---|---|---|
| `id` | TEXT | Yes | No | — | — |
| `account_id` | TEXT | No | No | — | vendor_accounts.id |
| `network_server_id` | TEXT | No | No | — | network_servers.id |
| `kind` | TEXT | No | Yes | — | — |
| `amount_cents` | INTEGER | No | Yes | — | — |
| `start_date` | TEXT | No | Yes | — | — |
| `end_date` | TEXT | No | Yes | — | — |
| `charge_day` | INTEGER | No | No | — | — |
| `provenance` | TEXT | No | Yes | 'user_recorded' | — |
| `account_revision` | INTEGER | No | No | — | — |
| `receipt_revision` | INTEGER | No | No | — | — |
| `receipt_login_seq` | INTEGER | No | No | — | — |
| `supersedes_id` | TEXT | No | No | — | vendor_account_billing_facts.id |
| `created_at` | TEXT | No | Yes | — | — |

Indexes: `idx_vendor_account_billing_facts_account` (account_id, kind, start_date, end_date, created_at), `idx_vendor_account_billing_facts_server` (network_server_id, kind, start_date, end_date, created_at), `idx_vendor_account_billing_facts_supersedes` (supersedes_id, unique), `sqlite_autoindex_vendor_account_billing_facts_1` (id, unique).

### `vendor_account_billing_revisions`

| Column | Type | Primary key | NOT NULL | Default | References ||---|---|---|---|---|---|
| `account_id` | TEXT | No | No | — | vendor_accounts.id |
| `network_server_id` | TEXT | No | No | — | network_servers.id |
| `revision` | INTEGER | No | Yes | 0 | — |

Indexes: `idx_vendor_account_billing_revisions_account` (account_id, unique), `idx_vendor_account_billing_revisions_server` (network_server_id, unique).

### `vendor_accounts`

| Column | Type | Primary key | NOT NULL | Default | References ||---|---|---|---|---|---|
| `id` | TEXT | Yes | No | — | — |
| `connection_id` | TEXT | No | Yes | — | connections.id |
| `name` | TEXT | No | Yes | — | — |
| `source_home` | TEXT | No | No | — | — |
| `runtime_home` | TEXT | No | No | — | — |
| `is_default` | INTEGER | No | Yes | 0 | — |
| `state` | TEXT | No | Yes | 'signed_out' | — |
| `revision` | INTEGER | No | Yes | 0 | — |
| `identity_json` | TEXT | No | Yes | '{}' | — |
| `auth_cleanup_json` | TEXT | No | No | — | — |
| `discovery_json` | TEXT | No | No | — | — |
| `usage_json` | TEXT | No | No | — | — |
| `created_at` | TEXT | No | Yes | — | — |
| `updated_at` | TEXT | No | Yes | — | — |
| `removed_at` | TEXT | No | No | — | — |

Indexes: `idx_vendor_accounts_connection` (connection_id, created_at), `idx_vendor_accounts_default` (connection_id, unique), `sqlite_autoindex_vendor_accounts_1` (id, unique).
