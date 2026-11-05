# Live Store

Tables in the document store created by `backend/doceditor/live_store.py`.

## Tables 1–4

```mermaid
erDiagram
    live {
        INTEGER seq PK
        BLOB upd
    }
    meta {
        INTEGER singleton PK
        TEXT store_id
        TEXT failures
        INTEGER failure_generation
    }
    mirror {
        INTEGER seq PK
        BLOB state
        TEXT file_hash
    }
    pending {
        INTEGER singleton PK
        TEXT op_id
        BLOB candidate
        BLOB mirror_state
        TEXT prior_hash
        TEXT result_hash
        TEXT state
        TEXT outside_hash
        TEXT choice
        BLOB choice_candidate
        BLOB choice_mirror
        TEXT choice_prior_hash
        TEXT choice_result_hash
        TEXT publication
        TEXT choice_publication
    }
```

### `live`

| Column | Type | Primary key | NOT NULL | Default | References ||---|---|---|---|---|---|
| `seq` | INTEGER | Yes | No | — | — |
| `upd` | BLOB | No | Yes | — | — |

Indexes: None.

### `meta`

| Column | Type | Primary key | NOT NULL | Default | References ||---|---|---|---|---|---|
| `singleton` | INTEGER | Yes | No | — | — |
| `store_id` | TEXT | No | Yes | — | — |
| `failures` | TEXT | No | Yes | '{}' | — |
| `failure_generation` | INTEGER | No | Yes | 0 | — |

Indexes: None.

### `mirror`

| Column | Type | Primary key | NOT NULL | Default | References ||---|---|---|---|---|---|
| `seq` | INTEGER | Yes | No | — | — |
| `state` | BLOB | No | Yes | — | — |
| `file_hash` | TEXT | No | Yes | — | — |

Indexes: None.

### `pending`

| Column | Type | Primary key | NOT NULL | Default | References ||---|---|---|---|---|---|
| `singleton` | INTEGER | Yes | No | — | — |
| `op_id` | TEXT | No | Yes | — | — |
| `candidate` | BLOB | No | Yes | — | — |
| `mirror_state` | BLOB | No | Yes | — | — |
| `prior_hash` | TEXT | No | Yes | — | — |
| `result_hash` | TEXT | No | Yes | — | — |
| `state` | TEXT | No | Yes | — | — |
| `outside_hash` | TEXT | No | No | — | — |
| `choice` | TEXT | No | No | — | — |
| `choice_candidate` | BLOB | No | No | — | — |
| `choice_mirror` | BLOB | No | No | — | — |
| `choice_prior_hash` | TEXT | No | No | — | — |
| `choice_result_hash` | TEXT | No | No | — | — |
| `publication` | TEXT | No | No | — | — |
| `choice_publication` | TEXT | No | No | — | — |

Indexes: `sqlite_autoindex_pending_1` (op_id, unique).

## Tables 5–5

```mermaid
erDiagram
    publish {
        INTEGER seq PK
        TEXT publication
        BLOB mirror_state
        TEXT result_hash
    }
```

### `publish`

| Column | Type | Primary key | NOT NULL | Default | References ||---|---|---|---|---|---|
| `seq` | INTEGER | Yes | No | — | — |
| `publication` | TEXT | No | Yes | — | — |
| `mirror_state` | BLOB | No | Yes | — | — |
| `result_hash` | TEXT | No | Yes | — | — |

Indexes: None.
