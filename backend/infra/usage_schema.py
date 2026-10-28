"""Canonical model-pricing and LLM-usage ledger schema."""

from __future__ import annotations

from infra.limit_history_schema import LIMIT_HISTORY_SCHEMA_SQL
from infra.usage_rollup_schema import USAGE_ROLLUP_SQL

USAGE_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS model_pricing (
        id TEXT PRIMARY KEY,
        model TEXT NOT NULL,
        effective_date TEXT NOT NULL,
        input_cost_per_token REAL NOT NULL DEFAULT 0.0,
        output_cost_per_token REAL NOT NULL DEFAULT 0.0,
        cache_creation_cost_per_token REAL NOT NULL DEFAULT 0.0,
        cache_read_cost_per_token REAL NOT NULL DEFAULT 0.0,
        source TEXT NOT NULL DEFAULT 'litellm_sync',
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_model_pricing_lookup
        ON model_pricing(model, effective_date DESC);

    CREATE TABLE IF NOT EXISTS llm_usage (
        id TEXT PRIMARY KEY,
        thread_id TEXT NOT NULL,
        turn_id TEXT,
        vendor_account_id TEXT REFERENCES vendor_accounts(id),
        model TEXT NOT NULL,
        prompt_tokens INTEGER DEFAULT 0,
        completion_tokens INTEGER DEFAULT 0,
        cache_creation_tokens INTEGER DEFAULT 0,
        cache_read_tokens INTEGER DEFAULT 0,
        total_tokens INTEGER DEFAULT 0,
        cost_usd REAL DEFAULT 0.0,
        ttft_ms INTEGER,
        plan_id TEXT,
        plan_node_id TEXT,
        plan_attempt_id TEXT,
        plan_commitment_id TEXT,
        plan_usage_role TEXT CHECK (
            plan_usage_role IS NULL OR plan_usage_role IN (
                'execution','delegation','verification','correction'
            )
        ),
        source TEXT NOT NULL DEFAULT 'conversation',
        created_at TEXT NOT NULL,
        context_manifest_id TEXT,
        reasoning_effort TEXT,
        CHECK (
            (plan_id IS NULL AND plan_node_id IS NULL AND plan_attempt_id IS NULL
             AND plan_commitment_id IS NULL AND plan_usage_role IS NULL)
            OR
            (plan_id IS NOT NULL AND plan_node_id IS NOT NULL
             AND plan_attempt_id IS NOT NULL AND plan_commitment_id IS NOT NULL
             AND plan_usage_role IS NOT NULL)
        )
    );
    CREATE INDEX IF NOT EXISTS idx_llm_usage_session
        ON llm_usage(thread_id, created_at);
    CREATE INDEX IF NOT EXISTS idx_llm_usage_commitment
        ON llm_usage(plan_commitment_id, created_at);
    CREATE INDEX IF NOT EXISTS idx_llm_usage_model
        ON llm_usage(model, created_at);
    CREATE INDEX IF NOT EXISTS idx_llm_usage_source
        ON llm_usage(source, created_at);
    CREATE INDEX IF NOT EXISTS idx_llm_usage_plan_node
        ON llm_usage(plan_node_id) WHERE plan_node_id IS NOT NULL;
    CREATE INDEX IF NOT EXISTS idx_active_turns_plan_node
        ON active_turns(plan_node_id) WHERE plan_node_id IS NOT NULL;
    CREATE INDEX IF NOT EXISTS idx_llm_usage_context_manifest
        ON llm_usage(context_manifest_id) WHERE context_manifest_id IS NOT NULL;

    -- One content-addressed guidance list (core.guidance_manifest): refs and
    -- hashes only, never prompt text. Kept as long as the usage it explains.
    CREATE TABLE IF NOT EXISTS context_manifests (
        id TEXT PRIMARY KEY,
        harness TEXT NOT NULL,
        selector TEXT,
        resolved_model TEXT,
        resolved_model_source TEXT,
        entries_json TEXT NOT NULL CHECK (json_valid(entries_json)),
        entry_count INTEGER NOT NULL,
        created_at TEXT NOT NULL
    );

    -- One mid-Turn read of a SKILL.md through read_file.
    CREATE TABLE IF NOT EXISTS guidance_loads (
        id TEXT PRIMARY KEY,
        thread_id TEXT,
        turn_id TEXT,
        path TEXT NOT NULL,
        raw_sha256 TEXT NOT NULL,
        git_blob_oid TEXT NOT NULL,
        delivered_sha256 TEXT NOT NULL,
        byte_count INTEGER NOT NULL,
        partial INTEGER NOT NULL CHECK (partial IN (0, 1)),
        created_at TEXT NOT NULL,
        node_id TEXT,
        variant TEXT CHECK (variant IS NULL OR variant IN ('with', 'without', 'crossover')),
        checkpoint_generation_id TEXT,
        lesson_id TEXT,
        lesson_behavioral_sha256 TEXT,
        omission_reason TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_guidance_loads_turn
        ON guidance_loads(turn_id) WHERE turn_id IS NOT NULL;
    CREATE INDEX IF NOT EXISTS idx_guidance_loads_lesson_node
        ON guidance_loads(lesson_id, lesson_behavioral_sha256, node_id) WHERE lesson_id IS NOT NULL;
    CREATE INDEX IF NOT EXISTS idx_guidance_loads_trial_thread_time
        ON guidance_loads(thread_id, created_at, lesson_id, lesson_behavioral_sha256, id)
        WHERE variant='with' AND lesson_id IS NOT NULL AND lesson_behavioral_sha256 IS NOT NULL;
""" + LIMIT_HISTORY_SCHEMA_SQL
# The hourly rollup Measurement reads in place of the ledger (``api.usage_rollup``).
USAGE_SCHEMA_SQL += USAGE_ROLLUP_SQL
