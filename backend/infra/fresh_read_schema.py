"""Fresh reads: the check an agent's item passes before it reaches the person.

``core.fresh_read`` writes and reads these rows.

- ``fresh_reads``: one row per read (or per person edit that moves a document's
  pass). ``sha256`` is the hash of the exact text the person sees; ``verdict`` is
  ``passed``, ``failed``, ``held`` (no reader could run) or ``person_edit`` (the
  person's own save, which needs no read). ``findings`` and ``attempts`` are JSON.
- ``fresh_read_bytes``: the bytes of each document version a view still points at,
  keyed by their sha256, so the person is shown exactly what passed.
- ``fresh_read_views``: per document, the version the person is shown and who holds
  its pass. ``recheck_sha256``/``recheck_verdict`` record the last background read of
  a newer version that did not pass, so the passed copy becomes editable.
"""

from __future__ import annotations

FRESH_READ_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS fresh_reads (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        item_ref TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        verdict TEXT NOT NULL CHECK (verdict IN ('passed', 'failed', 'held', 'person_edit')),
        findings TEXT,
        attempts TEXT,
        writer_thread_id TEXT,
        node_id TEXT,
        reader_model TEXT,
        input_cap INTEGER,
        messages_cut_from TEXT,
        elapsed_ms INTEGER,
        prompt_tokens INTEGER,
        completion_tokens INTEGER,
        cost_usd REAL,
        cost_source TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_fresh_reads_item
        ON fresh_reads(kind, item_ref, sha256, verdict);
    CREATE TABLE IF NOT EXISTS fresh_read_bytes (
        sha256 TEXT PRIMARY KEY,
        body BLOB NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS fresh_read_views (
        artifact_id TEXT PRIMARY KEY,
        sha256 TEXT NOT NULL,
        fresh_read_id TEXT NOT NULL,
        holder_thread_id TEXT,
        node_id TEXT,
        recheck_sha256 TEXT,
        recheck_verdict TEXT,
        updated_at TEXT NOT NULL
    );
"""
