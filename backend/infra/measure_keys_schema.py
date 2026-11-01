"""Measure keys: what lets an outside tool write declared events to the OTLP receiver.

A key is stored only as its SHA-256 and shown once. It resolves on the server to the
account, the agent thread and the class that minted it; the payload never says which.
``agent`` keys are minted by the agent and readable to it; ``emitter`` is reserved for a
key the person mints for a product outside the agent's reach, which nothing mints yet.
"""

MEASURE_KEYS_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS measure_keys (
        key_id          TEXT PRIMARY KEY,
        key_hash        TEXT NOT NULL UNIQUE,
        user_id         TEXT NOT NULL,
        agent_thread_id TEXT NOT NULL,
        class           TEXT NOT NULL CHECK (class IN ('agent', 'emitter')),
        label           TEXT NOT NULL,
        created_at      TEXT NOT NULL,
        revoked_at      TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_measure_keys_agent
        ON measure_keys(agent_thread_id, created_at);
"""
