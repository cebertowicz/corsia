
from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from infra.schema import SCHEMA_SQL  # noqa: E402



NEW_MODELS_COLUMNS = {
    "last_used_at",
    "runtime_state",
    "last_error",
    "load_started_at",
    "load_completed_at",
    "inference_overrides",
    "load_overrides",
    "estimated_memory_gb",
}
NEW_LLM_USAGE_COLUMNS = {"ttft_ms"}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def _legacy_models_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE models (
            id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            provider_model_id TEXT NOT NULL,
            litellm_model TEXT NOT NULL,
            display_name TEXT NOT NULL,
            backend TEXT NOT NULL DEFAULT 'api',
            source TEXT NOT NULL DEFAULT 'seed',
            enabled INTEGER NOT NULL DEFAULT 1,
            available INTEGER NOT NULL DEFAULT 1,
            metadata_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(provider, provider_model_id)
        );
        CREATE TABLE llm_usage (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0.0,
            source TEXT NOT NULL DEFAULT 'conversation',
            created_at TEXT NOT NULL
        );
    """)
    conn.commit()



def test_fresh_schema_has_models_v054_columns():
    conn = _make_conn()
    conn.executescript(SCHEMA_SQL)
    cols = _columns(conn, "models")
    missing = NEW_MODELS_COLUMNS - cols
    assert not missing, f"models table missing columns: {missing}"


def test_fresh_schema_has_canonical_harness_axis():
    conn = _make_conn()
    conn.executescript(SCHEMA_SQL)
    columns = {
        row[1]: row for row in conn.execute("PRAGMA table_info(models)").fetchall()
    }
    assert columns["harness"][3] == 1
    assert columns["harness"][4] == "'native'"

    conn.execute(
        "INSERT INTO models (id, provider, provider_model_id, litellm_model, "
        "display_name, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        ("native", "openai", "gpt-x", "openai/gpt-x", "GPT-X", "t", "t"),
    )
    assert conn.execute(
        "SELECT harness FROM models WHERE id='native'"
    ).fetchone()[0] == "native"


def test_fresh_schema_has_llm_usage_v054_columns():
    conn = _make_conn()
    conn.executescript(SCHEMA_SQL)
    cols = _columns(conn, "llm_usage")
    missing = NEW_LLM_USAGE_COLUMNS - cols
    assert not missing, f"llm_usage table missing columns: {missing}"


def test_runtime_state_default_is_idle():
    conn = _make_conn()
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        "INSERT INTO models (id, provider, provider_model_id, litellm_model, "
        "display_name, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("m1", "anthropic", "claude-haiku", "anthropic/claude-haiku",
         "Haiku", "2026-04-25T00:00:00Z", "2026-04-25T00:00:00Z"),
    )
    state = conn.execute("SELECT runtime_state FROM models WHERE id = 'm1'").fetchone()[0]
    assert state == "idle"



def test_json_overrides_round_trip():
    import json
    conn = _make_conn()
    conn.executescript(SCHEMA_SQL)
    overrides = {"temperature": 0.1, "top_p": 0.95, "max_tokens": 2048}
    conn.execute(
        "INSERT INTO models (id, provider, provider_model_id, litellm_model, "
        "display_name, inference_overrides, load_overrides, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("m1", "omlx", "qwen3.5-27b", "omlx@srv/qwen3.5-27b",
         "Model-3-5", json.dumps(overrides), json.dumps({"kv_quant": "q8"}),
         "2026-04-25T00:00:00Z", "2026-04-25T00:00:00Z"),
    )
    row = conn.execute(
        "SELECT inference_overrides, load_overrides FROM models WHERE id = 'm1'"
    ).fetchone()
    assert json.loads(row[0]) == overrides
    assert json.loads(row[1]) == {"kv_quant": "q8"}
