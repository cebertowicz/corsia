"""Measure keys: what lets an outside tool write declared events to the OTLP receiver.

A key is stored only as its SHA-256 and shown once. It resolves on the server to the
agent thread that minted it (whose account is ``threads.user_id``); the payload never says which. The agent mints
its own keys and can read their labels back, never the key.
"""

MEASURE_KEYS_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS measure_keys (
        key_id          TEXT PRIMARY KEY,
        key_hash        TEXT NOT NULL UNIQUE,
        agent_thread_id TEXT NOT NULL,
        label           TEXT NOT NULL,
        created_at      TEXT NOT NULL,
        revoked_at      TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_measure_keys_agent
        ON measure_keys(agent_thread_id, created_at);
"""
