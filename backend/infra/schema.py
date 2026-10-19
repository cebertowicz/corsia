"""SQLite schema DDL used by database initialization."""
from infra.accounts_schema import ACCOUNTS_SCHEMA_SQL
from infra.action_drafts_schema import ACTION_DRAFTS_SCHEMA_SQL
from infra.email_screens_schema import EMAIL_SCREENS_SCHEMA_SQL
from infra.artifact_identity_schema import ARTIFACT_IDENTITY_SCHEMA_SQL
from infra.bulletin_schema import BULLETIN_SCHEMA_SQL
from infra.connection_schema import CONNECTION_SCHEMA_SQL
from infra.connected_accounts_schema import CONNECTED_ACCOUNTS_SCHEMA_SQL
from infra.connected_apps_schema import CONNECTED_APPS_SCHEMA_SQL
from infra.vendor_policy_schema import VENDOR_SUBSCRIPTION_SCHEMA_SQL
from infra.checkpoint_schema import CHECKPOINT_SCHEMA_SQL
from infra.context_change_schema import CONTEXT_CHANGE_SCHEMA_SQL
from infra.doc_comments_schema import DOCUMENT_SCHEMA_SQL
from infra.continuation_schema import CONTINUATION_SCHEMA_SQL
from infra.continuity_schema import CONTINUITY_SCHEMA_SQL
from infra.continuity_staging import STAGING_SCHEMA_SQL
from infra.delivery_foundation_schema import DELIVERY_FOUNDATION_SCHEMA_SQL
from infra.host_access_schema import HOST_ACCESS_LEASE_SCHEMA_SQL
from infra.key_store_schema import KEY_STORE_SCHEMA_SQL
from infra.learning_schema import LEARNING_INDEX_SQL
from infra.lesson_placement_check_schema import LESSON_PLACEMENT_CHECK_SCHEMA_SQL
from infra.managed_process_schema import MANAGED_PROCESS_SCHEMA_SQL
from infra.measurement_schema import MEASUREMENT_INDEX_SQL
from infra.message_schema import MESSAGE_SCHEMA_SQL
from infra.outcome_events_schema import OUTCOME_EVENTS_SCHEMA_SQL
from infra.plan_liveness_schema import PLAN_LIVENESS_SCHEMA_SQL
from infra.plan_schema import STRUCTURED_PLAN_SCHEMA_SQL
from infra.presence_schema import PRESENCE_SCHEMA_SQL
from infra.project_policy_schema import PROJECT_INFERENCE_POLICY_TABLE_SQL, PROJECT_REPO_POLICY_TABLE_SQL
from infra.provider_availability_schema import PROVIDER_AVAILABILITY_TABLE_SQL
from infra.reviewer_gate_schema import REVIEWER_GATE_SCHEMA_SQL
from infra.settings_schema import SETTINGS_SCHEMA_SQL
from infra.thread_updates_schema import THREAD_UPDATES_SCHEMA_SQL
from infra.usage_schema import USAGE_SCHEMA_SQL
from infra.webhook_delivery_schema import WEBHOOK_DELIVERY_SCHEMA_SQL
from infra.write_hold_indexes import WRITE_HOLD_INDEX_SQL
SCHEMA_VERSION = 130
SCHEMA_SQL = f"""
    {SETTINGS_SCHEMA_SQL}
    {BULLETIN_SCHEMA_SQL}
    CREATE TABLE IF NOT EXISTS threads (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        profile TEXT DEFAULT 'personal',
        title TEXT DEFAULT 'New Chat',
        model TEXT,
        reasoning_effort TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        last_auto_retry_at TEXT,
        project_id TEXT REFERENCES projects(id),
        lifecycle_state TEXT DEFAULT 'active',
        dormant_at TEXT,
        archived_at TEXT,
        archived_reason TEXT,
        purged_at TEXT,
        pinned INTEGER DEFAULT 0,
        parent_thread_id TEXT REFERENCES threads(id),
        spawned_by TEXT REFERENCES threads(id),
        local_only INTEGER NOT NULL DEFAULT 0,
        local_only_inherited_from_thread_id TEXT REFERENCES threads(id),
        bootloader_files TEXT,
        pending_compaction_trigger TEXT,
        pending_compaction_detail TEXT,
        agent_enabled INTEGER DEFAULT 0,
        agent_responsibility TEXT,
        agent_paths TEXT,
        agent_domains TEXT,
        agent_status TEXT DEFAULT 'active',
        agent_notes TEXT,
        agent_roster_updated_at TEXT NOT NULL DEFAULT '1970-01-01T00:00:00+00:00',
        history_epoch INTEGER NOT NULL DEFAULT 0,
        runtime_context_epoch INTEGER NOT NULL DEFAULT 0 CHECK (typeof(runtime_context_epoch) = 'integer' AND runtime_context_epoch >= 0),
        last_context_change_seq INTEGER NOT NULL DEFAULT 0
            CHECK (typeof(last_context_change_seq) = 'integer' AND last_context_change_seq >= 0),
        execution_epoch INTEGER NOT NULL DEFAULT 0
            CHECK (typeof(execution_epoch) = 'integer' AND execution_epoch >= 0),
        pending_model TEXT,
        pending_execution_epoch INTEGER
            CHECK (pending_execution_epoch IS NULL OR (
                typeof(pending_execution_epoch) = 'integer'
                AND pending_execution_epoch > execution_epoch
            )),
        switch_after_input_order INTEGER
            CHECK (switch_after_input_order IS NULL OR (
                typeof(switch_after_input_order) = 'integer'
                AND switch_after_input_order >= 0
            )),
        switch_operation_id TEXT,
        switch_status TEXT
            CHECK (switch_status IS NULL OR switch_status IN (
                'preparing', 'ready', 'failed'
            )),
        switch_error TEXT,
        switch_requested_at TEXT,
        switch_actor_kind TEXT,
        switch_actor_id TEXT,
        switch_reason TEXT,
        switch_checkpoint_choice TEXT
            CHECK (switch_checkpoint_choice IS NULL OR switch_checkpoint_choice IN (
                'current', 'target'
            )),
        next_input_order INTEGER NOT NULL DEFAULT 1
            CHECK (typeof(next_input_order) = 'integer' AND next_input_order > 0),
        next_message_seq INTEGER NOT NULL DEFAULT 1
    );
    {STRUCTURED_PLAN_SCHEMA_SQL}
    {CONTINUATION_SCHEMA_SQL}
    {MESSAGE_SCHEMA_SQL}
    {DELIVERY_FOUNDATION_SCHEMA_SQL}
    {CHECKPOINT_SCHEMA_SQL}
    {STAGING_SCHEMA_SQL}
    {CONTINUITY_SCHEMA_SQL}
    {PRESENCE_SCHEMA_SQL}
    {THREAD_UPDATES_SCHEMA_SQL}{ACTION_DRAFTS_SCHEMA_SQL}{EMAIL_SCREENS_SCHEMA_SQL}
    {OUTCOME_EVENTS_SCHEMA_SQL}
    {LESSON_PLACEMENT_CHECK_SCHEMA_SQL}
    {PROVIDER_AVAILABILITY_TABLE_SQL};
    CREATE TABLE IF NOT EXISTS model_profiles (
        model_id TEXT PRIMARY KEY,
        context_window INTEGER,
        temperature REAL,
        parallel_slots INTEGER DEFAULT 1,
        reasoning_budget INTEGER DEFAULT -1,
        flash_attention INTEGER DEFAULT 1,
        custom_args TEXT,
        max_tokens INTEGER,
        updated_at TEXT
    );
        CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(thread_id, created_at);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_session_seq
        ON messages(thread_id, seq);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_input_order
        ON messages(thread_id, input_order) WHERE input_order IS NOT NULL;
    CREATE INDEX IF NOT EXISTS idx_messages_tool_lineage
        ON messages(
            thread_id,
            role,
            json_extract(metadata, '$.assistant_step_id'),
            json_extract(metadata, '$.tool_call_id')
        ) WHERE role='tool' AND json_valid(metadata);
    -- The declaring half of that same lineage. Every Turn terminal settles the
    -- calls it declared, so that read must seek the Turn's own Steps; without
    -- it the read scans the whole thread and taxes every response on a long one.
    CREATE INDEX IF NOT EXISTS idx_messages_turn_calls
        ON messages(
            thread_id,
            role,
            json_extract(metadata, '$.turn_id')
        ) WHERE role='assistant' AND json_valid(metadata);
    CREATE INDEX IF NOT EXISTS idx_threads_user ON threads(user_id, updated_at DESC);
    CREATE TABLE IF NOT EXISTS active_turns (
        turn_order INTEGER PRIMARY KEY AUTOINCREMENT,
        id TEXT NOT NULL UNIQUE,
        thread_id TEXT NOT NULL REFERENCES threads(id),
        execution_epoch INTEGER NOT NULL
            CHECK (typeof(execution_epoch) = 'integer' AND execution_epoch >= 0),
        model TEXT NOT NULL CHECK (length(trim(model)) > 0),
        status TEXT NOT NULL DEFAULT 'running'
            CHECK (status IN (
                'running', 'waiting_retry', 'completed', 'cancelled', 'error'
            )),
        started_at TEXT NOT NULL,
        ended_at TEXT,
        error TEXT,
        boot_epoch TEXT NOT NULL CHECK (length(trim(boot_epoch)) > 0),
        retry_at TEXT,
        idle_checkpoint_pending INTEGER NOT NULL DEFAULT 0
            CHECK (typeof(idle_checkpoint_pending)='integer' AND idle_checkpoint_pending IN (0,1)),
        usage_receipts_json TEXT NOT NULL DEFAULT '{{}}'
            CHECK (
                json_valid(usage_receipts_json)
                AND json_type(usage_receipts_json)='object'
            ),
        plan_id TEXT,
        plan_node_id TEXT,
        plan_attempt_id TEXT,
        plan_commitment_id TEXT,
        plan_usage_role TEXT CHECK (
            plan_usage_role IS NULL OR plan_usage_role IN (
                'execution','delegation','verification','correction'
            )
        ),
        vendor_account_id TEXT REFERENCES vendor_accounts(id),
        context_manifest_id TEXT, guidance_load_status TEXT,
        context_manifest_status TEXT DEFAULT 'unbound',
        CHECK (status != 'waiting_retry' OR retry_at IS NOT NULL),
        CHECK (
            (plan_id IS NULL AND plan_node_id IS NULL AND plan_attempt_id IS NULL
             AND plan_commitment_id IS NULL AND plan_usage_role IS NULL)
            OR
            (plan_id IS NOT NULL AND plan_node_id IS NOT NULL
             AND plan_attempt_id IS NOT NULL AND plan_commitment_id IS NOT NULL
             AND plan_usage_role IS NOT NULL)
        )
    );
    CREATE INDEX IF NOT EXISTS idx_active_turns_status ON active_turns(status);
    CREATE INDEX IF NOT EXISTS idx_active_turns_session ON active_turns(thread_id);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_active_turns_one_active
        ON active_turns(thread_id)
        WHERE status IN ('running','waiting_retry');
    CREATE INDEX IF NOT EXISTS idx_active_turns_idle_checkpoint_due
        ON active_turns(ended_at,turn_order) WHERE idle_checkpoint_pending=1;
    CREATE INDEX IF NOT EXISTS idx_active_turns_pending_usage
        ON active_turns(turn_order) WHERE usage_receipts_json <> '{{}}';
    CREATE TABLE IF NOT EXISTS context_bootloader_events (
        id TEXT PRIMARY KEY,
        actor_user_id TEXT,
        actor_thread_id TEXT REFERENCES threads(id),
        scope TEXT NOT NULL,
        target_id TEXT NOT NULL,
        action TEXT NOT NULL,
        files_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_context_bootloader_events_target
        ON context_bootloader_events(scope, target_id, created_at DESC);
    CREATE TABLE IF NOT EXISTS projects (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        path TEXT,
        description TEXT,
        status TEXT DEFAULT 'active',
        accent_color TEXT,
        doc_roots TEXT,
        bootloader_files TEXT,
        conductor_thread_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status);
    {PROJECT_INFERENCE_POLICY_TABLE_SQL}; {PROJECT_REPO_POLICY_TABLE_SQL};
    {HOST_ACCESS_LEASE_SCHEMA_SQL}
    {MANAGED_PROCESS_SCHEMA_SQL}
    {USAGE_SCHEMA_SQL}
    {ACCOUNTS_SCHEMA_SQL}
    {KEY_STORE_SCHEMA_SQL}
    {CONNECTED_APPS_SCHEMA_SQL}
    {CONNECTED_ACCOUNTS_SCHEMA_SQL}
    CREATE TABLE IF NOT EXISTS fact_extraction_runs (
        run_date TEXT PRIMARY KEY,
        status TEXT NOT NULL DEFAULT 'ok',
        result_json TEXT,
        ran_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS network_servers (
        id TEXT PRIMARY KEY,
        provider TEXT NOT NULL DEFAULT 'omlx',
        kind TEXT NOT NULL DEFAULT 'managed',
        dialect TEXT,
        instance_id TEXT NOT NULL,
        machine_id TEXT,
        instance_name TEXT NOT NULL,
        port INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'offline',
        source TEXT DEFAULT 'manual',
        latency_ms INTEGER,
        memory_total_mb INTEGER,
        memory_used_mb INTEGER,
        gpu_info TEXT,
        metadata_json TEXT,
        last_seen_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(provider, instance_id)
    );
    CREATE INDEX IF NOT EXISTS idx_network_servers_provider ON network_servers(provider, status);

    CREATE TABLE IF NOT EXISTS network_server_endpoints (
        id TEXT PRIMARY KEY,
        server_id TEXT NOT NULL REFERENCES network_servers(id) ON DELETE CASCADE,
        host TEXT NOT NULL,
        kind TEXT NOT NULL,
        last_ok_at TEXT,
        last_latency_ms INTEGER,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(server_id, host)
    );
    CREATE INDEX IF NOT EXISTS idx_network_endpoints_server ON network_server_endpoints(server_id);

    CREATE TABLE IF NOT EXISTS models (
        id TEXT PRIMARY KEY,
        harness TEXT NOT NULL DEFAULT 'native',
        provider TEXT NOT NULL,
        provider_model_id TEXT NOT NULL,
        litellm_model TEXT NOT NULL,
        display_name TEXT NOT NULL,
        alias TEXT,
        description TEXT,
        backend TEXT NOT NULL DEFAULT 'api',
        source TEXT NOT NULL DEFAULT 'seed',
        enabled INTEGER NOT NULL DEFAULT 1,
        available INTEGER NOT NULL DEFAULT 1,
        context_window INTEGER,
        supports_vision INTEGER DEFAULT 0,
        supports_tools INTEGER DEFAULT 1,
        supports_reasoning INTEGER DEFAULT 0,
        default_temperature REAL DEFAULT 0.3,
        default_max_tokens INTEGER DEFAULT 8192,
        metadata_json TEXT,
        last_used_at TEXT,
        runtime_state TEXT DEFAULT 'idle',
        last_error TEXT,
        load_started_at TEXT,
        load_completed_at TEXT,
        inference_overrides TEXT,
        load_overrides TEXT,
        estimated_memory_gb REAL,
        discovered_at TEXT,
        last_seen_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(provider, provider_model_id)
    );
    {WEBHOOK_DELIVERY_SCHEMA_SQL}

    CREATE TABLE IF NOT EXISTS dead_letter_events (
        id TEXT PRIMARY KEY,
        internal_event TEXT NOT NULL,
        external_id TEXT,
        reason TEXT NOT NULL,
        payload_json TEXT,
        status TEXT NOT NULL DEFAULT 'parked',
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_dead_letter_status
        ON dead_letter_events(status, created_at);

    {CONNECTION_SCHEMA_SQL}
    {VENDOR_SUBSCRIPTION_SCHEMA_SQL}

    -- Reliability automations (coordination MVP S7): expected-event watches arm
    -- from concrete conditions, persist, resolve once, expire once. The dedupe
    -- index keys an armed watch as one thread's wait on one artifact's event.
    CREATE TABLE IF NOT EXISTS expected_event_watches (
        id TEXT PRIMARY KEY,
        artifact_integration TEXT,
        artifact_type TEXT,
        artifact_external_id TEXT,
        owner_thread_id TEXT,
        plan_id TEXT REFERENCES plans(id),
        plan_node_id TEXT REFERENCES plan_nodes(id),
        armed_by_actor_id TEXT,
        expected_event TEXT,
        condition_type TEXT NOT NULL,
        condition_value TEXT,
        status TEXT NOT NULL DEFAULT 'armed',
        armed_at TEXT NOT NULL,
        resolved_at TEXT,
        expired_at TEXT,
        routed_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_watches_status
        ON expected_event_watches(status, condition_type);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_watches_dedupe ON expected_event_watches
        (artifact_integration, artifact_type, artifact_external_id, owner_thread_id,
         condition_type, condition_value, expected_event) WHERE status='armed';

    -- Restart drain registrations (coordination MVP S7). Active work registers
    -- for a post-restart wake; the drain wakes each once or surfaces it loud.
    CREATE TABLE IF NOT EXISTS restart_wake_registrations (
        id TEXT PRIMARY KEY,
        thread_id TEXT NOT NULL,
        reason TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        registered_at TEXT NOT NULL,
        drained_at TEXT,
        operation_id TEXT,
        turn_id TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_restart_registrations_status
        ON restart_wake_registrations(status);
    CREATE INDEX IF NOT EXISTS idx_restart_registrations_turn
        ON restart_wake_registrations(operation_id, turn_id, status);

    -- Sanctioned self-restart control (Governance, 2026-07). Every
    -- agent-initiated "restart this instance" records a durable audit row
    -- (requester + serving SHA being replaced) that doubles as the in-flight
    -- concurrency guard: a new request is refused while an 'in_flight' row
    -- younger than the TTL exists. Post-restart startup marks any surviving
    -- 'in_flight' row 'superseded' and stamps the deployed SHA, closing the
    -- audit loop. An instance the operator has not opted in never writes here:
    -- the control endpoint refuses unless the operator flag is set.
    CREATE TABLE IF NOT EXISTS self_restart_requests (
        id TEXT PRIMARY KEY,
        requester_thread_id TEXT,
        requester_user TEXT,
        from_sha TEXT,
        deployed_sha TEXT,
        branch TEXT,
        reason TEXT,
        status TEXT NOT NULL DEFAULT 'in_flight',
        requested_at TEXT NOT NULL,
        settled_at TEXT,
        phase TEXT,
        cutoff_at TEXT,
        updated_at TEXT,
        error TEXT,
        kind TEXT, trigger TEXT, target_version TEXT, drain_total INTEGER,
        changes TEXT, outcome_seen_at TEXT, failure_code TEXT, failure_detail TEXT,
        conductor_told_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_self_restart_requests_status ON self_restart_requests(status);
    CREATE INDEX IF NOT EXISTS idx_self_restart_requests_update_attempts ON self_restart_requests(kind, requested_at);
    {PLAN_LIVENESS_SCHEMA_SQL}

    {ARTIFACT_IDENTITY_SCHEMA_SQL}
    -- tmp is retired/inert (superseded by artifact_identities.retention); kept
    -- only because SQLite ALTER TABLE cannot drop a column in place.
    CREATE TABLE IF NOT EXISTS artifact_registrations (
        id TEXT PRIMARY KEY,
        artifact_id TEXT NOT NULL REFERENCES artifact_identities(id),
        parent_thread_id TEXT NOT NULL,
        relationship_state TEXT NOT NULL DEFAULT 'owner-active',
        tmp INTEGER NOT NULL DEFAULT 0,
        surfacing TEXT NOT NULL DEFAULT 'visible' CHECK (surfacing IN ('visible', 'quiet', 'settled')),
        origin_plan_node_id TEXT, retired_at TEXT,
        metadata TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(artifact_id, parent_thread_id)
    );
    CREATE INDEX IF NOT EXISTS idx_artifact_registrations_thread
        ON artifact_registrations(parent_thread_id, updated_at DESC);
    CREATE INDEX IF NOT EXISTS idx_artifact_registrations_artifact
        ON artifact_registrations(artifact_id, relationship_state);

    -- Minimal filesystem/registry handoff journal. A durable shared write
    -- cannot be one transaction across SQLite and the filesystem, so startup
    -- reconciles the publication phase and durable undo marker before serving.
    CREATE TABLE IF NOT EXISTS durable_write_intents (
        id TEXT PRIMARY KEY,
        operation TEXT NOT NULL CHECK (operation IN ('write', 'promote')),
        phase TEXT NOT NULL CHECK (phase IN ('staged', 'file_applied')),
        actor_thread_id TEXT NOT NULL,
        artifact_type TEXT NOT NULL,
        external_id TEXT NOT NULL,
        target_path TEXT NOT NULL UNIQUE,
        stage_path TEXT NOT NULL,
        backup_path TEXT,
        source_path TEXT,
        description TEXT,
        content_sha256 TEXT NOT NULL,
        created_parent INTEGER NOT NULL DEFAULT 0 CHECK (created_parent IN (0,1)),
        -- 0=normal, 1=undo pending/applying, 2=undo applied/cleanup pending.
        undoing INTEGER NOT NULL DEFAULT 0 CHECK (undoing IN (0,1,2)),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    {CONTEXT_CHANGE_SCHEMA_SQL}

    -- Append-only artifact registration / ownership audit. Current relationship
    -- state lives in artifact_registrations; release history also preserves the
    -- prior-owner authority used by explicit re-home.
    CREATE TABLE IF NOT EXISTS artifact_registration_events (
        id TEXT PRIMARY KEY,
        artifact_id TEXT NOT NULL REFERENCES artifact_identities(id),
        event_type TEXT NOT NULL,
        actor_thread_id TEXT,
        authority TEXT,
        reason TEXT,
        source_path TEXT,
        causal_event_id TEXT,
        affected_thread_id TEXT,
        old_owner_thread_id TEXT,
        new_owner_thread_id TEXT,
        old_relationship_state TEXT,
        new_relationship_state TEXT,
        metadata TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_artifact_registration_events_artifact
        ON artifact_registration_events(artifact_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_artifact_registration_events_thread
        ON artifact_registration_events(affected_thread_id, created_at DESC);

    -- Idempotent receipts for artifact_reconcile apply: one row per
    -- (artifact, identity version, owner session) action so a replayed apply
    -- never double-mutates lifecycle.
    CREATE TABLE IF NOT EXISTS artifact_reconcile_receipts (
        id TEXT PRIMARY KEY,
        artifact_id TEXT NOT NULL REFERENCES artifact_identities(id),
        identity_version TEXT NOT NULL,
        thread_id TEXT NOT NULL,
        action TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('applied', 'skipped', 'failed')),
        reason TEXT,
        created_at TEXT NOT NULL,
        UNIQUE(artifact_id, identity_version, thread_id)
    );
    CREATE INDEX IF NOT EXISTS idx_artifact_reconcile_receipts_session
        ON artifact_reconcile_receipts(thread_id, created_at DESC);

    -- Durable recovery journal for artifact_relocate: filesystem rename and registry
    -- identity update are not one transaction, so an interrupted relocation is
    -- deterministically completed or cleaned up on discovery. target_type/target_title
    -- carry the FULL target identity so recovery reaches the same end state as the
    -- uninterrupted path; they are nullable only so a pre-existing in-flight intent can
    -- still be recovered (the writer always populates them).
    CREATE TABLE IF NOT EXISTS artifact_relocation_intents (
        id TEXT PRIMARY KEY,
        artifact_id TEXT NOT NULL REFERENCES artifact_identities(id),
        actor_thread_id TEXT NOT NULL,
        source_path TEXT NOT NULL,
        target_path TEXT NOT NULL,
        source_external_id TEXT NOT NULL,
        target_external_id TEXT NOT NULL,
        target_type TEXT,
        target_title TEXT,
        phase TEXT NOT NULL CHECK (phase IN ('recorded', 'file_moved', 'identity_updated')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    {REVIEWER_GATE_SCHEMA_SQL}
    {DOCUMENT_SCHEMA_SQL}
    {MEASUREMENT_INDEX_SQL}
    {WRITE_HOLD_INDEX_SQL}
    {LEARNING_INDEX_SQL}
"""
