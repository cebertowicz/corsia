#!/usr/bin/env python3
"""Create a disposable, schema-shaped state root for the opt-in adoption rehearsal.

This never reads an existing CORSIA root. The caller must provide a nonexistent
or empty directory below a disposable parent, then removes that parent after the
rehearsal. The fixture is intentionally expensive: it exercises SQLite's real
schema and FTS triggers with production-shaped message and checkpoint content.
"""
from __future__ import annotations

import argparse
import hashlib
import sqlite3
from pathlib import Path

from infra.database import initialize_schema

MIB = 1024 * 1024
MESSAGE_ROWS = 3_040_000
MESSAGE_BYTES = 5_000
CHECKPOINT_BYTES = 1_050
CHECKPOINT_ROWS = 3_040_000


def require_empty(path: Path) -> None:
    """Refuse an existing nonempty target so this tool cannot overwrite state."""
    if path.exists() and any(path.iterdir()):
        raise SystemExit(f"refusing nonempty fixture root: {path}")
    path.mkdir(parents=True, exist_ok=True)


def payload(prefix: str, size: int) -> str:
    """Make deterministic, compress-resistant enough text for SQLite and FTS."""
    seed = hashlib.sha256(prefix.encode()).hexdigest()
    return (f"{prefix} {seed} " * (size // (len(prefix) + len(seed) + 2) + 1))[:size]


def build(root: Path) -> None:
    """Build the current schema and fill its production-shaped tables through FTS triggers."""
    require_empty(root)
    database = root / "corsia.db"
    with sqlite3.connect(database) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        initialize_schema(conn)
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            "INSERT INTO threads (id, user_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("rehearsal-thread", "rehearsal-user", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        message = payload("synthetic adoption rehearsal message", MESSAGE_BYTES)
        checkpoint = payload("synthetic adoption rehearsal checkpoint", CHECKPOINT_BYTES)
        for start in range(0, MESSAGE_ROWS, 10_000):
            stop = min(start + 10_000, MESSAGE_ROWS)
            conn.executemany(
                "INSERT INTO messages (id, thread_id, role, content, created_at) VALUES (?, ?, 'assistant', ?, ?)",
                ((f"message-{number}", "rehearsal-thread", message, "2026-01-01T00:00:00+00:00") for number in range(start, stop)),
            )
            conn.commit()
        for start in range(0, CHECKPOINT_ROWS, 10_000):
            stop = min(start + 10_000, CHECKPOINT_ROWS)
            conn.executemany(
                "INSERT INTO checkpoints (id, thread_id, history_epoch, through_seq, through_message_id, trigger, mode, status, source_harness, source_model, writer_harness, writer_model, continuity_base_revision, continuity_base_snapshot_json, raw_result, started_at) VALUES (?, ?, 0, ?, ?, 'manual', 'warm', 'installed', 'rehearsal', 'rehearsal', 'rehearsal', 'rehearsal', 0, '{}', ?, ?)",
                ((f"checkpoint-{number}", "rehearsal-thread", number + 1, f"message-{number}", checkpoint, "2026-01-01T00:00:00+00:00") for number in range(start, stop)),
            )
            conn.commit()
        conn.execute("INSERT INTO messages_fts(messages_fts) VALUES ('optimize')")
        conn.execute("INSERT INTO checkpoints_fts(checkpoints_fts) VALUES ('optimize')")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    for version in (126, 130):
        backup = root / f"corsia.db.pre-migration-v{version}-20260101T000000Z.bak"
        with sqlite3.connect(database) as source, sqlite3.connect(backup) as target:
            source.backup(target)
    import duckdb

    with duckdb.connect(root / "analytics.duckdb") as analytics:
        analytics.execute("CREATE TABLE rehearsal (payload VARCHAR)")
        analytics.executemany(
            "INSERT INTO rehearsal VALUES (?)",
            ((payload(f"analytics-{number}", MIB),) for number in range(1024)),
        )


def main() -> None:
    """Parse the one explicit synthetic-root argument and build the fixture."""
    parser = argparse.ArgumentParser()
    parser.add_argument("state_root", type=Path)
    args = parser.parse_args()
    build(args.state_root.resolve())


if __name__ == "__main__":
    main()
