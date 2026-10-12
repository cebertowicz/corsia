-- Frozen canonical dev schema78 from c843d66978c3c9d37a6f088522baf20bf673353b.
-- Generated from that commit, including its initialize_schema FTS definitions.

    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS threads (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        profile TEXT DEFAULT 'personal',
        title TEXT DEFAULT 'New Chat',
        title_source TEXT DEFAULT 'auto',
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
            CHECK (
                typeof(last_context_change_seq) = 'integer'
                AND last_context_change_seq >= 0
            ),
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

    CREATE TABLE IF NOT EXISTS plans (
        id TEXT PRIMARY KEY,
        owner_kind TEXT NOT NULL
            CHECK (owner_kind IN ('thread', 'project_scope', 'workspace_scope')),
        owner_id TEXT NOT NULL CHECK (length(trim(owner_id)) > 0),
        parent_plan_id TEXT REFERENCES plans(id),
        state TEXT NOT NULL DEFAULT 'active'
            CHECK (state IN ('active', 'paused', 'complete', 'archived')),
        revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        archived_at TEXT,
        UNIQUE(owner_kind, owner_id)
    );
    CREATE INDEX IF NOT EXISTS idx_plans_parent ON plans(parent_plan_id);

    CREATE TABLE IF NOT EXISTS plan_nodes (
        id TEXT PRIMARY KEY,
        plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
        parent_id TEXT REFERENCES plan_nodes(id) ON DELETE CASCADE,
        order_key INTEGER NOT NULL CHECK (order_key >= 0),
        kind TEXT NOT NULL CHECK (kind IN ('phase', 'track', 'task')),
        title TEXT NOT NULL CHECK (length(trim(title)) > 0),
        description TEXT,
        workflow_ref TEXT,
        workflow_version TEXT,
        workflow_step TEXT,
        difficulty TEXT CHECK (
            difficulty IS NULL OR difficulty IN ('routine', 'standard', 'hard')
        ),
        acceptance_criteria TEXT NOT NULL
            CHECK (length(trim(acceptance_criteria)) > 0),
        checkpoint_exception INTEGER NOT NULL DEFAULT 0
            CHECK (checkpoint_exception IN (0, 1)),
        state TEXT NOT NULL DEFAULT 'pending'
            CHECK (state IN (
                'pending', 'active', 'review_requested', 'accepted',
                'accepted_with_followup', 'failed', 'closed'
            )),
        assignee TEXT,
        spawn_kind TEXT,
        owner_thread_id TEXT REFERENCES threads(id),
        executor_thread_id TEXT REFERENCES threads(id),
        spawned_thread_id TEXT REFERENCES threads(id),
        follow_up_of TEXT REFERENCES plan_nodes(id),
        requirements_revision INTEGER NOT NULL DEFAULT 1
            CHECK (requirements_revision > 0),
        commitment_id TEXT NOT NULL REFERENCES plan_nodes(id),
        watchdog_budget_seconds INTEGER
            CHECK (watchdog_budget_seconds IS NULL OR watchdog_budget_seconds > 0),
        revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        archived_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_plan_nodes_tree
        ON plan_nodes(plan_id, parent_id, order_key);
    CREATE INDEX IF NOT EXISTS idx_plan_nodes_follow_up
        ON plan_nodes(follow_up_of);
    CREATE INDEX IF NOT EXISTS idx_plan_nodes_commitment
        ON plan_nodes(commitment_id, created_at);
    -- A wait reads a step's child, and an executor's work, by thread.
    CREATE INDEX IF NOT EXISTS idx_plan_nodes_spawned_thread ON plan_nodes(spawned_thread_id);
    CREATE INDEX IF NOT EXISTS idx_plan_nodes_executor_thread ON plan_nodes(executor_thread_id);

    CREATE TABLE IF NOT EXISTS plan_node_dependencies (
        node_id TEXT NOT NULL REFERENCES plan_nodes(id) ON DELETE CASCADE,
        depends_on_node_id TEXT NOT NULL
            REFERENCES plan_nodes(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL,
        PRIMARY KEY (node_id, depends_on_node_id),
        CHECK (node_id <> depends_on_node_id)
    ) WITHOUT ROWID;
    CREATE INDEX IF NOT EXISTS idx_plan_node_dependencies_target
        ON plan_node_dependencies(depends_on_node_id);

    CREATE TABLE IF NOT EXISTS plan_node_attempts (
        id TEXT PRIMARY KEY,
        plan_id TEXT NOT NULL REFERENCES plans(id),
        node_id TEXT NOT NULL REFERENCES plan_nodes(id),
        executor_thread_id TEXT NOT NULL REFERENCES threads(id),
        requirements_revision INTEGER NOT NULL CHECK (requirements_revision > 0),
        attempt_ordinal INTEGER NOT NULL CHECK (attempt_ordinal > 0),
        selected_by_actor_id TEXT,
        provenance TEXT NOT NULL CHECK (length(trim(provenance)) > 0),
        selected_at TEXT NOT NULL,
        released_at TEXT,
        release_reason TEXT,
        UNIQUE(node_id, attempt_ordinal),
        CHECK ((released_at IS NULL) = (release_reason IS NULL))
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_plan_attempt_open_node
        ON plan_node_attempts(node_id) WHERE released_at IS NULL;
    CREATE UNIQUE INDEX IF NOT EXISTS idx_plan_attempt_open_executor
        ON plan_node_attempts(plan_id, executor_thread_id)
        WHERE released_at IS NULL;
    CREATE INDEX IF NOT EXISTS idx_plan_attempt_commitment
        ON plan_node_attempts(node_id, selected_at);
    CREATE TRIGGER IF NOT EXISTS prevent_plan_attempt_delete
    BEFORE DELETE ON plan_node_attempts
    BEGIN
        SELECT RAISE(ABORT, 'plan attempts cannot be deleted');
    END;
    CREATE TRIGGER IF NOT EXISTS restrict_plan_attempt_update
    BEFORE UPDATE ON plan_node_attempts
    WHEN OLD.released_at IS NOT NULL
      OR NEW.id IS NOT OLD.id
      OR NEW.plan_id IS NOT OLD.plan_id
      OR NEW.node_id IS NOT OLD.node_id
      OR NEW.executor_thread_id IS NOT OLD.executor_thread_id
      OR NEW.requirements_revision IS NOT OLD.requirements_revision
      OR NEW.attempt_ordinal IS NOT OLD.attempt_ordinal
      OR NEW.selected_by_actor_id IS NOT OLD.selected_by_actor_id
      OR NEW.provenance IS NOT OLD.provenance
      OR NEW.selected_at IS NOT OLD.selected_at
      OR NEW.released_at IS NULL
      OR NEW.release_reason IS NULL
    BEGIN
        SELECT RAISE(ABORT, 'plan attempt identity and release are immutable');
    END;

    CREATE TABLE IF NOT EXISTS plan_tool_dispatches (
        id TEXT PRIMARY KEY,
        attempt_id TEXT NOT NULL REFERENCES plan_node_attempts(id),
        turn_id TEXT NOT NULL CHECK (length(trim(turn_id)) > 0),
        tool_name TEXT NOT NULL CHECK (length(trim(tool_name)) > 0),
        started_at TEXT NOT NULL,
        settled_at TEXT,
        outcome TEXT CHECK (
            outcome IS NULL OR outcome IN ('succeeded','refused','failed','unknown')
        ),
        result_message_id TEXT,
        CHECK ((settled_at IS NULL) = (outcome IS NULL))
    );
    CREATE INDEX IF NOT EXISTS idx_plan_tool_dispatch_open_attempt
        ON plan_tool_dispatches(attempt_id) WHERE settled_at IS NULL;
    CREATE INDEX IF NOT EXISTS idx_plan_tool_dispatch_turn
        ON plan_tool_dispatches(turn_id, started_at);
    CREATE TRIGGER IF NOT EXISTS prevent_plan_tool_dispatch_delete
    BEFORE DELETE ON plan_tool_dispatches
    BEGIN
        SELECT RAISE(ABORT, 'plan tool dispatches cannot be deleted');
    END;
    CREATE TRIGGER IF NOT EXISTS restrict_plan_tool_dispatch_update
    BEFORE UPDATE ON plan_tool_dispatches
    WHEN OLD.settled_at IS NOT NULL
      OR NEW.id IS NOT OLD.id
      OR NEW.attempt_id IS NOT OLD.attempt_id
      OR NEW.turn_id IS NOT OLD.turn_id
      OR NEW.tool_name IS NOT OLD.tool_name
      OR NEW.started_at IS NOT OLD.started_at
      OR NEW.settled_at IS NULL
      OR NEW.outcome IS NULL
    BEGIN
        SELECT RAISE(ABORT, 'plan tool dispatch identity and settlement are immutable');
    END;

    CREATE TABLE IF NOT EXISTS plan_node_events (
        id TEXT PRIMARY KEY,
        node_id TEXT NOT NULL REFERENCES plan_nodes(id),
        actor_id TEXT,
        provenance TEXT NOT NULL CHECK (length(trim(provenance)) > 0),
        event TEXT NOT NULL CHECK (length(trim(event)) > 0),
        prior_state TEXT,
        new_state TEXT,
        verification_result TEXT,
        causal_event_id TEXT REFERENCES plan_node_events(id),
        payload_json TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_plan_node_events_node
        ON plan_node_events(node_id, created_at);
    CREATE TRIGGER IF NOT EXISTS prevent_plan_node_event_update
    BEFORE UPDATE ON plan_node_events
    BEGIN
        SELECT RAISE(ABORT, 'plan node events are append-only');
    END;
    CREATE TRIGGER IF NOT EXISTS prevent_plan_node_event_delete
    BEFORE DELETE ON plan_node_events
    BEGIN
        SELECT RAISE(ABORT, 'plan node events are append-only');
    END;

    CREATE TABLE IF NOT EXISTS plan_node_holds (
        id TEXT PRIMARY KEY,
        node_id TEXT NOT NULL REFERENCES plan_nodes(id),
        kind TEXT NOT NULL CHECK (kind IN ('paused','blocked')),
        reason_key TEXT NOT NULL CHECK (length(trim(reason_key)) > 0),
        reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
        owner_kind TEXT NOT NULL CHECK (
            owner_kind IN ('user','thread','external','system')
        ),
        owner_id TEXT,
        created_by_actor_id TEXT,
        provenance TEXT NOT NULL CHECK (length(trim(provenance)) > 0),
        created_at TEXT NOT NULL,
        cleared_by_actor_id TEXT,
        cleared_at TEXT,
        clearance_event_id TEXT REFERENCES plan_node_events(id),
        CHECK (
            (cleared_by_actor_id IS NULL AND cleared_at IS NULL
             AND clearance_event_id IS NULL)
            OR
            (cleared_by_actor_id IS NOT NULL AND cleared_at IS NOT NULL
             AND clearance_event_id IS NOT NULL)
        )
    );
    CREATE INDEX IF NOT EXISTS idx_plan_holds_active_node
        ON plan_node_holds(node_id, kind) WHERE cleared_at IS NULL;
    CREATE UNIQUE INDEX IF NOT EXISTS idx_plan_holds_one_pause
        ON plan_node_holds(node_id) WHERE kind='paused' AND cleared_at IS NULL;
    CREATE TRIGGER IF NOT EXISTS prevent_plan_hold_delete
    BEFORE DELETE ON plan_node_holds
    BEGIN
        SELECT RAISE(ABORT, 'plan holds cannot be deleted');
    END;

    CREATE TRIGGER IF NOT EXISTS restrict_plan_hold_update
    BEFORE UPDATE ON plan_node_holds
    WHEN OLD.cleared_at IS NOT NULL
      OR NEW.id IS NOT OLD.id
      OR NEW.node_id IS NOT OLD.node_id
      OR NEW.kind IS NOT OLD.kind
      OR NEW.reason_key IS NOT OLD.reason_key
      OR NEW.reason IS NOT OLD.reason
      OR NEW.owner_kind IS NOT OLD.owner_kind
      OR NEW.owner_id IS NOT OLD.owner_id
      OR NEW.created_by_actor_id IS NOT OLD.created_by_actor_id
      OR NEW.provenance IS NOT OLD.provenance
      OR NEW.created_at IS NOT OLD.created_at
      OR NEW.cleared_by_actor_id IS NULL
      OR NEW.cleared_at IS NULL
      OR NEW.clearance_event_id IS NULL
    BEGIN
        SELECT RAISE(ABORT, 'plan hold identity and clearance are immutable');
    END;

    CREATE TABLE IF NOT EXISTS plan_node_artifact_links (
        node_id TEXT NOT NULL REFERENCES plan_nodes(id) ON DELETE CASCADE,
        artifact_id TEXT NOT NULL REFERENCES artifact_identities(id),
        role TEXT NOT NULL CHECK (role IN ('input','result','attention','discussion')),
        actor_id TEXT,
        provenance TEXT NOT NULL CHECK (length(trim(provenance)) > 0),
        source_event_id TEXT,
        created_at TEXT NOT NULL,
        PRIMARY KEY (node_id, artifact_id, role)
    ) WITHOUT ROWID;
    CREATE INDEX IF NOT EXISTS idx_plan_node_artifact_links_artifact
        ON plan_node_artifact_links(artifact_id, node_id);

    CREATE TRIGGER IF NOT EXISTS bump_plan_revision_node_insert
    AFTER INSERT ON plan_nodes
    BEGIN
        UPDATE plans SET revision=revision+1, updated_at=NEW.updated_at
        WHERE id=NEW.plan_id;
    END;
    CREATE TRIGGER IF NOT EXISTS bump_plan_revision_node_update
    AFTER UPDATE ON plan_nodes
    BEGIN
        UPDATE plans SET revision=revision+1, updated_at=NEW.updated_at
        WHERE id=NEW.plan_id;
    END;
    CREATE TRIGGER IF NOT EXISTS bump_plan_revision_node_delete
    AFTER DELETE ON plan_nodes
    BEGIN
        UPDATE plans SET revision=revision+1,
            updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
        WHERE id=OLD.plan_id;
    END;
    CREATE TRIGGER IF NOT EXISTS bump_plan_revision_dependency_insert
    AFTER INSERT ON plan_node_dependencies
    BEGIN
        UPDATE plans SET revision=revision+1,
            updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
        WHERE id=(SELECT plan_id FROM plan_nodes WHERE id=NEW.node_id);
    END;
    CREATE TRIGGER IF NOT EXISTS bump_plan_revision_dependency_delete
    AFTER DELETE ON plan_node_dependencies
    BEGIN
        UPDATE plans SET revision=revision+1,
            updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
        WHERE id=(SELECT plan_id FROM plan_nodes WHERE id=OLD.node_id);
    END;
    CREATE TRIGGER IF NOT EXISTS bump_plan_revision_link_insert
    AFTER INSERT ON plan_node_artifact_links
    BEGIN
        UPDATE plans SET revision=revision+1,
            updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
        WHERE id=(SELECT plan_id FROM plan_nodes WHERE id=NEW.node_id);
    END;
    CREATE TRIGGER IF NOT EXISTS bump_plan_revision_link_delete
    AFTER DELETE ON plan_node_artifact_links
    BEGIN
        UPDATE plans SET revision=revision+1,
            updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
        WHERE id=(SELECT plan_id FROM plan_nodes WHERE id=OLD.node_id);
    END;

    CREATE TABLE IF NOT EXISTS plan_mutation_receipts (
        plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
        mutation_id TEXT NOT NULL CHECK (length(trim(mutation_id)) > 0),
        actor_id TEXT NOT NULL CHECK (length(trim(actor_id)) > 0),
        operation TEXT NOT NULL CHECK (length(trim(operation)) > 0),
        request_fingerprint TEXT NOT NULL CHECK (length(request_fingerprint) = 64),
        result_json TEXT NOT NULL CHECK (length(trim(result_json)) > 0),
        created_at TEXT NOT NULL,
        PRIMARY KEY (plan_id, mutation_id)
    ) WITHOUT ROWID;
    CREATE TRIGGER IF NOT EXISTS prevent_plan_mutation_receipt_update
    BEFORE UPDATE ON plan_mutation_receipts
    BEGIN
        SELECT RAISE(ABORT, 'plan mutation receipts are immutable and append-only');
    END;
    CREATE TRIGGER IF NOT EXISTS prevent_plan_mutation_receipt_delete
    BEFORE DELETE ON plan_mutation_receipts
    BEGIN
        SELECT RAISE(ABORT, 'plan mutation receipts are immutable and append-only');
    END;

    CREATE TABLE IF NOT EXISTS plan_markdown_imports (
        id TEXT PRIMARY KEY,
        artifact_id TEXT NOT NULL UNIQUE REFERENCES artifact_identities(id),
        observed_owner_thread_id TEXT,
        owner_thread_id TEXT REFERENCES threads(id),
        registration_id TEXT,
        plan_id TEXT UNIQUE,
        integration TEXT NOT NULL,
        artifact_type TEXT NOT NULL,
        external_id TEXT NOT NULL,
        source_path TEXT NOT NULL,
        artifact_updated_at TEXT NOT NULL,
        registration_updated_at TEXT,
        registration_state TEXT,
        source_sha256 TEXT CHECK (
            source_sha256 IS NULL OR length(source_sha256) = 64
        ),
        parser_version TEXT,
        manifest_json TEXT,
        source_map_json TEXT,
        status TEXT NOT NULL CHECK (
            status IN (
                'discovered', 'repair_required', 'ready',
                'imported', 'rolled_back'
            )
        ),
        applied_plan_hash TEXT,
        wake_snapshot_json TEXT,
        discovered_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        imported_at TEXT,
        rolled_back_at TEXT
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_plan_markdown_imports_owner
        ON plan_markdown_imports(owner_thread_id)
        WHERE owner_thread_id IS NOT NULL;
    CREATE INDEX IF NOT EXISTS idx_plan_markdown_imports_status
        ON plan_markdown_imports(status);

    CREATE TABLE IF NOT EXISTS plan_markdown_import_issues (
        id TEXT PRIMARY KEY,
        import_id TEXT NOT NULL
            REFERENCES plan_markdown_imports(id) ON DELETE CASCADE,
        issue_order INTEGER NOT NULL CHECK (issue_order >= 0),
        code TEXT NOT NULL CHECK (length(trim(code)) > 0),
        message TEXT NOT NULL CHECK (length(trim(message)) > 0),
        source_line INTEGER CHECK (source_line IS NULL OR source_line > 0),
        detail_json TEXT,
        created_at TEXT NOT NULL,
        UNIQUE(import_id, issue_order)
    );
    CREATE INDEX IF NOT EXISTS idx_plan_markdown_import_issues_import
        ON plan_markdown_import_issues(import_id, issue_order);

    CREATE TABLE IF NOT EXISTS plan_events (
        id TEXT PRIMARY KEY,
        plan_id TEXT NOT NULL REFERENCES plans(id),
        actor_id TEXT,
        provenance TEXT NOT NULL CHECK (length(trim(provenance)) > 0),
        event TEXT NOT NULL CHECK (length(trim(event)) > 0),
        prior_state TEXT,
        new_state TEXT,
        payload_json TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_plan_events_plan
        ON plan_events(plan_id, created_at);
    CREATE TRIGGER IF NOT EXISTS prevent_plan_event_update
    BEFORE UPDATE ON plan_events
    BEGIN
        SELECT RAISE(ABORT, 'plan events are append-only');
    END;
    CREATE TRIGGER IF NOT EXISTS prevent_plan_event_delete
    BEFORE DELETE ON plan_events
    BEGIN
        SELECT RAISE(ABORT, 'plan events are append-only');
    END;

    -- One row per Turn that ran at least one work-tool call with no plan task
    -- selected (``plans.tool_dispatches``). Written once, at Turn terminal,
    -- from an in-process counter: a mid-Turn process restart or a failed
    -- write loses that Turn, so totals here are a lower bound, never exact.
    -- Like ``plan_tool_dispatches``, a row is audit data that outlives its
    -- thread: nothing deletes plan records with a thread, so ``thread_id``
    -- declares no foreign key it could not keep.
    CREATE TABLE IF NOT EXISTS plan_unattributed_dispatches (
        turn_id TEXT PRIMARY KEY,
        thread_id TEXT NOT NULL,
        call_count INTEGER NOT NULL CHECK (call_count > 0),
        plan_state TEXT NOT NULL CHECK (plan_state IN ('active', 'paused', 'none')),
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_plan_unattributed_dispatches_thread
        ON plan_unattributed_dispatches(thread_id, created_at);

    CREATE TABLE IF NOT EXISTS plan_pane_views (
        user_id TEXT NOT NULL,
        thread_id TEXT NOT NULL REFERENCES threads(id),
        seen_through TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (user_id, thread_id)
    );


    CREATE TABLE IF NOT EXISTS continuation_triggers (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN (
            't0_liveness', 't1_watchdog', 't2_watch_expiry',
            'restart_resume', 'automation_clock', 'plan_parent'
        )),
        thread_id TEXT NOT NULL REFERENCES threads(id),
        scope_id TEXT NOT NULL,
        subject_id TEXT,
        dedupe_key TEXT NOT NULL,
        idempotency_key TEXT,
        state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN (
            'pending', 'dispatched', 'delivered', 'deferred', 'suppressed',
            'stalled', 'cancelled', 'failed'
        )),
        reason TEXT,
        attempt INTEGER NOT NULL DEFAULT 0,
        deferrals INTEGER NOT NULL DEFAULT 0,
        cost_estimate_usd REAL NOT NULL DEFAULT 0.0,
        next_attempt_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        dispatched_at TEXT,
        settled_at TEXT
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_continuation_triggers_live
        ON continuation_triggers(dedupe_key)
        WHERE state IN ('pending', 'dispatched', 'deferred');
    CREATE INDEX IF NOT EXISTS idx_continuation_triggers_state
        ON continuation_triggers(state, next_attempt_at);
    CREATE INDEX IF NOT EXISTS idx_continuation_triggers_scope
        ON continuation_triggers(scope_id, created_at);

    CREATE TABLE IF NOT EXISTS automation_schedules (
        id TEXT PRIMARY KEY,
        thread_id TEXT NOT NULL REFERENCES threads(id),
        scope_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('clock', 'cadence')),
        spec TEXT NOT NULL,
        next_run_at TEXT NOT NULL,
        last_run_at TEXT,
        runs INTEGER NOT NULL DEFAULT 0,
        max_runs INTEGER NOT NULL CHECK (max_runs > 0),
        state TEXT NOT NULL DEFAULT 'active'
            CHECK (state IN ('active', 'paused', 'cancelled', 'exhausted')),
        payload_json TEXT,
        created_by_actor TEXT NOT NULL,
        asked_by_user INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_automation_schedules_due
        ON automation_schedules(state, next_run_at);
    CREATE INDEX IF NOT EXISTS idx_automation_schedules_scope
        ON automation_schedules(scope_id, next_run_at);


    CREATE TABLE IF NOT EXISTS messages (
        id TEXT PRIMARY KEY,
        thread_id TEXT NOT NULL REFERENCES threads(id),
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        metadata TEXT,
        created_at TEXT NOT NULL,
        delivery_state TEXT NOT NULL DEFAULT 'delivered'
            CHECK (delivery_state IN ('queued', 'delivered', 'ambiguous', 'cancelled', 'failed')),
        delivery_error TEXT,
        delivery_revision INTEGER NOT NULL DEFAULT 1
            CHECK (typeof(delivery_revision) = 'integer' AND delivery_revision > 0),
        input_order INTEGER
            CHECK (input_order IS NULL OR (
                typeof(input_order) = 'integer' AND input_order > 0
            )),
        delivery_intent TEXT
            CHECK (delivery_intent IS NULL OR delivery_intent IN (
                'apply_now', 'queue_after_turn'
            )),
        target_execution_epoch INTEGER
            CHECK (target_execution_epoch IS NULL OR (
                typeof(target_execution_epoch) = 'integer'
                AND target_execution_epoch >= 0
            )),
        target_model TEXT,
        -- The condition a queued plan wake was collected for, judged again when
        -- a Turn would claim it. Outside the payload FIFO admission compares
        -- for duplicate identity, so a redelivery is still the same input.
        continuation_condition TEXT CHECK (continuation_condition IS NULL
            OR json_type(continuation_condition) = 'object'),
        delivery_options TEXT
            CHECK (delivery_options IS NULL OR (
                json_valid(delivery_options)
                AND json_type(delivery_options) = 'object'
            )),
        delivery_attempt_id TEXT,
        write_started_at TEXT,
        write_outcome TEXT
            CHECK (write_outcome IS NULL OR write_outcome IN (
                'accepted', 'proved_no_write', 'ambiguous'
            )),
        active_turn_id TEXT,
        next_attempt_at TEXT,
        seq INTEGER
    );


    CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_id_session
        ON messages(id, thread_id)
    ;

    CREATE INDEX IF NOT EXISTS idx_messages_delivery_attempt
        ON messages(delivery_attempt_id, input_order)
        WHERE delivery_attempt_id IS NOT NULL
    ;

    CREATE INDEX IF NOT EXISTS idx_messages_active_turn
        ON messages(active_turn_id, input_order)
        WHERE active_turn_id IS NOT NULL
    ;

    CREATE INDEX IF NOT EXISTS idx_messages_checkpoint_hold
        ON messages(thread_id, target_model, target_execution_epoch)
        WHERE delivery_state='queued'
          AND write_outcome='proved_no_write'
          AND active_turn_id IS NULL
          AND write_started_at IS NULL
;

CREATE TABLE IF NOT EXISTS checkpoints (
    generation_order INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    -- The cascade never fires at runtime: connections do not enable PRAGMA
    -- foreign_keys. Thread delete and purge remove these rows through
    -- infra.checkpoints.delete_thread_checkpoints (#2291). Migration 65 -> 66
    -- removed the rows earlier deletes left behind.
    thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    history_epoch INTEGER NOT NULL,
    previous_generation_id TEXT,
    through_seq INTEGER NOT NULL,
    through_message_id TEXT NOT NULL,
    trigger TEXT NOT NULL CHECK (trigger IN ('manual','pressure','model_switch','idle','agent')),
    mode TEXT NOT NULL CHECK (mode IN ('warm','cold')),
    status TEXT NOT NULL CHECK (
        status IN ('running','captured','installed','overtaken','failed','cancelled')
    ),
    source_harness TEXT NOT NULL,
    source_model TEXT NOT NULL,
    writer_harness TEXT NOT NULL,
    writer_model TEXT NOT NULL,
    originating_turn_id TEXT,
    first_included_seq INTEGER,
    continuity_base_revision INTEGER NOT NULL,
    continuity_base_snapshot_json TEXT NOT NULL,
    raw_result TEXT,
    continuity_snapshot_json TEXT,
    usage_json TEXT,
    error TEXT,
    started_at TEXT NOT NULL,
    captured_at TEXT,
    installed_at TEXT,
    ended_at TEXT,
    artifact_snapshot_json TEXT,
    plan_status_snapshot_json TEXT,
    -- The creation time of the boundary message this checkpoint's narrative
    -- covers (#3196): the rendered checkpoint dates its own history from it.
    -- NULL on rows installed before this column existed; never backfilled.
    through_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS checkpoints_one_live
    ON checkpoints(thread_id) WHERE status IN ('running','captured');
CREATE INDEX IF NOT EXISTS checkpoints_current
    ON checkpoints(thread_id,history_epoch,status,generation_order DESC);
CREATE INDEX IF NOT EXISTS checkpoints_listing
    ON checkpoints(thread_id,generation_order DESC);
CREATE INDEX IF NOT EXISTS checkpoints_idle_boundary
    ON checkpoints(thread_id,history_epoch,through_message_id) WHERE trigger='idle';

CREATE TABLE IF NOT EXISTS checkpoint_entry_search (
    -- The cascade never fires at runtime: connections do not enable PRAGMA
    -- foreign_keys. Thread delete and purge remove these rows through
    -- infra.checkpoints.delete_thread_checkpoints (#2291). Migration 65 -> 66
    -- removed the rows earlier deletes left behind.
    thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    history_epoch INTEGER NOT NULL,
    entry_id INTEGER NOT NULL CHECK (entry_id > 0),
    revision INTEGER NOT NULL CHECK (revision >= 0),  -- 0: entry carried none
    field TEXT NOT NULL,
    search_text TEXT NOT NULL,
    first_generation_order INTEGER NOT NULL
        REFERENCES checkpoints(generation_order) ON DELETE CASCADE,
    last_generation_order INTEGER NOT NULL
        REFERENCES checkpoints(generation_order) ON DELETE CASCADE,
    PRIMARY KEY (thread_id, history_epoch, entry_id, revision)
);
CREATE INDEX IF NOT EXISTS checkpoint_entry_search_identity
    ON checkpoint_entry_search(thread_id, history_epoch, entry_id);


CREATE TABLE IF NOT EXISTS checkpoint_staged_edits (
    checkpoint_id TEXT NOT NULL REFERENCES checkpoints(id) ON DELETE CASCADE,
    edit_order INTEGER NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('add','replace','drop','review')),
    entry_id INTEGER NOT NULL CHECK (entry_id > 0),
    field TEXT NOT NULL,
    body TEXT CHECK (body IS NULL OR json_valid(body)),
    reason TEXT,
    source_ref TEXT,
    base_entry_revision INTEGER,
    outcome TEXT NOT NULL DEFAULT 'pending'
        CHECK (outcome IN ('pending','applied','conflict')),
    outcome_detail TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at TEXT,
    PRIMARY KEY (checkpoint_id, edit_order)
);
CREATE INDEX IF NOT EXISTS checkpoint_staged_edits_target
    ON checkpoint_staged_edits(checkpoint_id,entry_id,edit_order);


    CREATE TABLE IF NOT EXISTS continuity_store (
        thread_id TEXT NOT NULL
            REFERENCES threads(id) ON DELETE CASCADE,
        history_epoch INTEGER NOT NULL,
        -- What a staged fold patch is compared against. Bumped on every write,
        -- so a fold that read revision N cannot land after someone else wrote.
        revision INTEGER NOT NULL DEFAULT 0,
        -- Ids are small integers so a model can address them in a tool call.
        next_entry_id INTEGER NOT NULL DEFAULT 1,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (thread_id, history_epoch)
    )
    ;

    CREATE TABLE IF NOT EXISTS continuity_entries (
        thread_id TEXT NOT NULL
            REFERENCES threads(id) ON DELETE CASCADE,
        history_epoch INTEGER NOT NULL,
        entry_id INTEGER NOT NULL CHECK (entry_id > 0),
        field TEXT NOT NULL,
        body TEXT NOT NULL CHECK (json_valid(body)),
        revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
        state TEXT NOT NULL DEFAULT 'active'
            CHECK (state IN ('active', 'retired')),
        -- A retirement carries why, and the evidence for why. Both or neither.
        retired_reason TEXT,
        retired_source_ref TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        -- The checkpoint placement mark (core/placement_marks.py). A mark
        -- applies to one entry revision; a later revision is unmarked again.
        placement_state TEXT CHECK (placement_state IS NULL OR placement_state IN (
            'reaffirmed','selected','written','already','proposed','stays',
            'refused'
        )),
        placement_revision INTEGER,
        placement_detail TEXT,
        PRIMARY KEY (thread_id, history_epoch, entry_id),
        CHECK (
            (state = 'active' AND retired_reason IS NULL)
            OR (state = 'retired' AND length(trim(retired_reason)) > 0)
        )
    )
    ;

    CREATE INDEX IF NOT EXISTS idx_continuity_entries_active
        ON continuity_entries(thread_id, history_epoch, field, state)
    ;

    CREATE TABLE IF NOT EXISTS thread_presence (
        user_id TEXT NOT NULL,
        thread_id TEXT NOT NULL REFERENCES threads(id),
        present INTEGER NOT NULL DEFAULT 0 CHECK (present IN (0, 1)),
        last_entered_at TEXT NOT NULL,
        last_exited_at TEXT,
        previous_entered_at TEXT,
        -- The exit time this visit will record: back-dated to the moment the
        -- user stopped being here (hidden, last input, last confirmation).
        exit_pending_at TEXT,
        -- When the server detected that exit. The merge window runs from here,
        -- never from the back-dated exit_pending_at.
        exit_pending_since TEXT,
        exit_pending_reason TEXT CHECK (exit_pending_reason IS NULL
            OR exit_pending_reason IN (
                'navigated', 'hidden', 'idle', 'disconnected', 'restart'
            )),
        last_confirmed_at TEXT NOT NULL,
        turn_order_at_exit INTEGER,
        away_seconds INTEGER,
        turns_while_away INTEGER,
        updated_at TEXT NOT NULL,
        -- Seen state for the update dot: Mark read raises the seen boundary
        -- to this time; Mark unread keeps the dot on until a visit opens.
        marked_read_at TEXT,
        marked_unread_at TEXT,
        PRIMARY KEY (user_id, thread_id)
    );
    CREATE INDEX IF NOT EXISTS idx_thread_presence_thread
        ON thread_presence(thread_id);
    CREATE TABLE IF NOT EXISTS presence_visits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        surface TEXT NOT NULL CHECK (length(trim(surface)) > 0),
        thread_id TEXT,
        project_id TEXT,
        object_id TEXT,
        entered_at TEXT NOT NULL,
        exited_at TEXT NOT NULL,
        seconds INTEGER NOT NULL CHECK (seconds >= 0),
        sessions INTEGER NOT NULL CHECK (sessions > 0),
        exit_reason TEXT NOT NULL CHECK (exit_reason IN (
            'navigated', 'hidden', 'idle', 'disconnected', 'restart'
        ))
    );
    CREATE INDEX IF NOT EXISTS idx_presence_visits_user_time
        ON presence_visits(user_id, entered_at);
    CREATE INDEX IF NOT EXISTS idx_presence_visits_thread
        ON presence_visits(thread_id, entered_at);


    CREATE TABLE IF NOT EXISTS thread_updates (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        thread_id   TEXT NOT NULL REFERENCES threads(id),
        kind        TEXT NOT NULL CHECK (kind IN (
                      'marked',
                      'done', 'done_follow_up', 'failed', 'closed', 'blocked')),
        source_kind TEXT NOT NULL CHECK (source_kind IN ('message', 'plan_event')),
        source_id   TEXT NOT NULL,
        -- Navigation only: no foreign key, so the card outlives its node.
        node_id     TEXT,
        title       TEXT,
        after_seq   INTEGER NOT NULL CHECK (after_seq >= 0),
        created_at  TEXT NOT NULL,
        UNIQUE (source_kind, source_id),
        CHECK ((kind = 'marked' AND source_kind = 'message') OR
               (kind != 'marked' AND source_kind = 'plan_event')),
        CHECK ((kind = 'marked' AND title IS NULL) OR
               (kind != 'marked' AND title IS NOT NULL AND length(trim(title)) > 0)),
        CHECK (kind = 'marked' OR node_id IS NOT NULL)
    );
    CREATE INDEX IF NOT EXISTS idx_thread_updates_thread_time
        ON thread_updates(thread_id, created_at);
    CREATE INDEX IF NOT EXISTS idx_thread_updates_thread_seq
        ON thread_updates(thread_id, after_seq);
    CREATE INDEX IF NOT EXISTS idx_thread_updates_node
        ON thread_updates(node_id, id) WHERE node_id IS NOT NULL;


    CREATE TABLE IF NOT EXISTS outcome_events (
        id              TEXT PRIMARY KEY,
        dedupe_key      TEXT NOT NULL UNIQUE CHECK (length(trim(dedupe_key)) > 0),
        event_name      TEXT NOT NULL CHECK (length(trim(event_name)) > 0),
        source          TEXT NOT NULL
                          CHECK (source IN ('github', 'corsia', 'agent', 'otlp')),
        provenance      TEXT NOT NULL
                          CHECK (provenance IN (
                            'observed', 'imported', 'instrumented', 'agent_recorded')),
        ts              TEXT NOT NULL,
        observed_ts     TEXT NOT NULL,
        subject_key     TEXT,
        project_id      TEXT,
        agent_thread_id TEXT,
        attribution     TEXT CHECK (attribution IS NULL OR attribution IN (
                          'title_prefix', 'body_owner', 'branch', 'caller', 'none')),
        corroborated_by TEXT,
        attributes      TEXT NOT NULL
                          CHECK (json_valid(attributes)
                                 AND json_type(attributes) = 'object'),
        retracted_at    TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_outcome_events_name_ts
        ON outcome_events(event_name, ts);
    CREATE INDEX IF NOT EXISTS idx_outcome_events_agent_ts
        ON outcome_events(agent_thread_id, ts);
    CREATE INDEX IF NOT EXISTS idx_outcome_events_project_ts
        ON outcome_events(project_id, ts);
    CREATE INDEX IF NOT EXISTS idx_outcome_events_subject
        ON outcome_events(subject_key);
    CREATE TABLE IF NOT EXISTS outcome_event_counters (
        counter    TEXT NOT NULL CHECK (counter IN (
                     'duplicate_ignored', 'attribute_dropped', 'mapping_failed')),
        event_name TEXT NOT NULL,
        n          INTEGER NOT NULL DEFAULT 0 CHECK (n >= 0),
        PRIMARY KEY (counter, event_name)
    );


    -- An availability observation is a dated claim about a provider that
    -- carries its own death. A row that cannot expire is UNWRITABLE, and
    -- expiry is evaluated by the READER: there is no refresher, no
    -- revalidator, and no write on the read path. An observation whose exit
    -- depends on another component running is exitless whenever that component
    -- is absent, crashed, or unconfigured — which is exactly what happened.
    CREATE TABLE IF NOT EXISTS provider_availability (
        harness TEXT NOT NULL CHECK (length(trim(harness)) > 0),
        provider TEXT NOT NULL CHECK (length(trim(provider)) > 0),
        account_scope TEXT NOT NULL CHECK (length(trim(account_scope)) > 0),
        -- 'available' is deleted. A positive latch is the same bug mirrored,
        -- and absence of a live row already means "no observed block".
        status TEXT NOT NULL
            CHECK (status IN ('transient_backoff','quota_exhausted')),
        observed_at TEXT NOT NULL CHECK (length(trim(observed_at)) > 0),
        reset_at TEXT CHECK (reset_at IS NULL OR length(trim(reset_at)) > 0),
        source TEXT NOT NULL CHECK (length(trim(source)) > 0),
        -- Rank so a transport guess cannot erase live provider evidence.
        -- provider_contract* = 2, transport:* = 1.
        evidence_rank INTEGER NOT NULL CHECK (evidence_rank IN (1, 2)),
        evidence_json TEXT NOT NULL
            CHECK (json_valid(evidence_json) AND json_type(evidence_json)='object'),
        -- Renamed from refresh_not_before, and the rename is load-bearing:
        -- "refresh_not_before" semantically licenses extension, which is how
        -- the read path legally pushed the block forward forever. "expires_at"
        -- means after this the row is not evidence, and nothing may extend it.
        expires_at TEXT NOT NULL CHECK (length(trim(expires_at)) > 0),
        updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0),
        PRIMARY KEY (harness, provider, account_scope),
        CHECK (
            (status='quota_exhausted' AND reset_at IS NOT NULL)
            OR (status!='quota_exhausted' AND reset_at IS NULL)
        ),
        CHECK (expires_at > observed_at),
        -- Bounded horizon per status: 6h quota, 15min transient.
        CHECK (
            julianday(expires_at) - julianday(observed_at)
            <= CASE status WHEN 'quota_exhausted' THEN 0.25 ELSE 0.0104166 END
        ),
        CHECK (status <> 'quota_exhausted' OR expires_at <= reset_at),
        CHECK (evidence_rank <> 2 OR source LIKE 'provider_contract%')
    )
;
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
        usage_receipts_json TEXT NOT NULL DEFAULT '{}'
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
        ON active_turns(turn_order) WHERE usage_receipts_json <> '{}';
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


    CREATE TABLE IF NOT EXISTS project_repo_policies (
        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        repo_name TEXT NOT NULL,
        repo_path TEXT NOT NULL,
        pinned_branch TEXT
            CHECK (pinned_branch IS NULL OR length(trim(pinned_branch)) > 0),
        protect_base_checkout INTEGER NOT NULL DEFAULT 1
            CHECK (protect_base_checkout IN (0, 1)),
        PRIMARY KEY (project_id, repo_name)
    )
;

    CREATE TABLE IF NOT EXISTS host_access_leases (
        id TEXT PRIMARY KEY,
        root_thread_id TEXT NOT NULL REFERENCES threads(id),
        user_id TEXT NOT NULL,
        state TEXT NOT NULL
            CHECK (
                state IN (
                    'reviewing',
                    'awaiting_user',
                    'activating',
                    'active',
                    'expired',
                    'revoked',
                    'blocked',
                    'rejected'
                )
            ),
        purpose TEXT NOT NULL CHECK (length(trim(purpose)) > 0),
        plan TEXT NOT NULL CHECK (length(trim(plan)) > 0),
        requested_duration_seconds INTEGER NOT NULL
            CHECK (
                requested_duration_seconds >= 1
                AND requested_duration_seconds <= 1800
            ),
        expected_resources TEXT,
        persistent_effects TEXT,
        rollback_plan TEXT,
        forbidden_effects TEXT,
        requesting_project_id TEXT,
        repo_policy_snapshot TEXT NOT NULL DEFAULT '[]',
        repo_policy_hash TEXT NOT NULL DEFAULT
            'ae3e2aca0c674a9150813698f7e5f14cd135a0dcd9324ead8044b85cd95ec70e',
        requested_policy_exceptions TEXT NOT NULL DEFAULT '[]',
        application_hash TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
        reviewer_model TEXT,
        application_review_result TEXT,
        application_reviewed_at TEXT,
        user_decision TEXT
            CHECK (
                user_decision IS NULL
                OR user_decision IN ('approved', 'rejected')
            ),
        user_decided_at TEXT,
        activated_at TEXT,
        activation_wake_delivered_at TEXT,
        expires_at TEXT,
        terminal_reason TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    ;

    CREATE INDEX IF NOT EXISTS idx_host_access_leases_root_state
        ON host_access_leases(root_thread_id, state)
    ;

    CREATE INDEX IF NOT EXISTS idx_host_access_leases_state_expiry
        ON host_access_leases(state, expires_at)
    ;

    CREATE UNIQUE INDEX IF NOT EXISTS idx_host_access_leases_one_nonterminal
        ON host_access_leases(root_thread_id)
        WHERE state IN ('reviewing', 'awaiting_user', 'activating', 'active')
    ;

    CREATE TRIGGER IF NOT EXISTS trg_host_access_lease_application_immutable
    BEFORE UPDATE ON host_access_leases
    WHEN
        NOT (NEW.root_thread_id IS OLD.root_thread_id)
        OR NOT (NEW.user_id IS OLD.user_id)
        OR NOT (NEW.purpose IS OLD.purpose)
        OR NOT (NEW.plan IS OLD.plan)
        OR NOT (
            NEW.requested_duration_seconds
            IS OLD.requested_duration_seconds
        )
        OR NOT (NEW.expected_resources IS OLD.expected_resources)
        OR NOT (NEW.persistent_effects IS OLD.persistent_effects)
        OR NOT (NEW.rollback_plan IS OLD.rollback_plan)
        OR NOT (NEW.forbidden_effects IS OLD.forbidden_effects)
        OR NOT (
            NEW.requesting_project_id IS OLD.requesting_project_id
        )
        OR NOT (NEW.repo_policy_snapshot IS OLD.repo_policy_snapshot)
        OR NOT (NEW.repo_policy_hash IS OLD.repo_policy_hash)
        OR NOT (
            NEW.requested_policy_exceptions IS OLD.requested_policy_exceptions
        )
        OR NOT (NEW.application_hash IS OLD.application_hash)
        OR NOT (NEW.created_at IS OLD.created_at)
    BEGIN
        SELECT RAISE(ABORT, 'host-access lease application is immutable');
    END
    ;

    CREATE TABLE IF NOT EXISTS host_access_action_reviews (
        id TEXT PRIMARY KEY,
        lease_id TEXT NOT NULL
            REFERENCES host_access_leases(id) ON DELETE CASCADE,
        lease_revision INTEGER NOT NULL CHECK (lease_revision > 0),
        thread_id TEXT NOT NULL REFERENCES threads(id),
        tool_name TEXT NOT NULL CHECK (length(trim(tool_name)) > 0),
        action_hash TEXT NOT NULL,
        redacted_descriptor TEXT NOT NULL,
        reviewer_model TEXT NOT NULL,
        verdict TEXT NOT NULL CHECK (verdict IN ('allow', 'block', 'revoke')),
        reason TEXT NOT NULL,
        permit_id TEXT,
        permit_expires_at TEXT,
        permit_consumed_at TEXT,
        created_at TEXT NOT NULL
    )
    ;

    CREATE INDEX IF NOT EXISTS idx_host_access_action_reviews_lease
        ON host_access_action_reviews(lease_id, created_at)
    ;

    CREATE INDEX IF NOT EXISTS idx_host_access_action_reviews_action
        ON host_access_action_reviews(lease_id, action_hash, created_at)
    ;

    CREATE UNIQUE INDEX IF NOT EXISTS idx_host_access_action_reviews_permit
        ON host_access_action_reviews(permit_id)
        WHERE permit_id IS NOT NULL
    ;

    CREATE TABLE IF NOT EXISTS managed_process_handles (
        handle TEXT PRIMARY KEY,
        thread_id TEXT NOT NULL,
        turn_id TEXT NOT NULL,
        lease_id TEXT,
        description TEXT,
        long_running INTEGER NOT NULL DEFAULT 0 CHECK (long_running IN (0, 1)),
        pid INTEGER NOT NULL,
        pgid INTEGER NOT NULL,
        creation_fingerprint TEXT NOT NULL,
        log_path TEXT NOT NULL,
        state TEXT NOT NULL,
        outcome TEXT,
        reason TEXT,
        exit_code INTEGER,
        started_at TEXT NOT NULL,
        last_output_at TEXT,
        stalled_since TEXT,
        stall_after_seconds INTEGER,
        max_lifetime_seconds INTEGER,
        ended_at TEXT,
        log_truncated INTEGER NOT NULL DEFAULT 0 CHECK (log_truncated IN (0, 1)),
        event_message_id TEXT
    )
    ;

    CREATE INDEX IF NOT EXISTS idx_managed_process_handles_thread
        ON managed_process_handles(thread_id, started_at)
    ;

    CREATE INDEX IF NOT EXISTS idx_managed_process_handles_state
        ON managed_process_handles(state)
    ;

    -- Deliberately no thread foreign key: this row owns post-delete file cleanup.
    CREATE TABLE IF NOT EXISTS managed_process_retirements (
        handle TEXT PRIMARY KEY,
        thread_id TEXT NOT NULL,
        head_log_name TEXT NOT NULL,
        tail_log_name TEXT,
        staged_at TEXT NOT NULL
    )
    ;

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
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_guidance_loads_turn
        ON guidance_loads(turn_id) WHERE turn_id IS NOT NULL;


    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        password_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        password_changed_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS auth_tokens (
        token_hash TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK (kind IN ('session', 'api', 'service')),
        label TEXT,
        created_at TEXT NOT NULL,
        expires_at TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_auth_tokens_user
        ON auth_tokens(user_id, created_at DESC);

    CREATE INDEX IF NOT EXISTS idx_auth_tokens_expiry
        ON auth_tokens(expires_at) WHERE expires_at IS NOT NULL;


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

    CREATE TABLE IF NOT EXISTS webhook_deliveries (
        id TEXT PRIMARY KEY,
        event_type TEXT NOT NULL,
        action TEXT,
        delivery_id TEXT NOT NULL UNIQUE,
        repo TEXT,
        payload_hash TEXT NOT NULL,
        payload_json TEXT,
        status TEXT NOT NULL DEFAULT 'received',
        error TEXT,
        created_at TEXT NOT NULL,
        object_key TEXT,
        check_suite_id INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_status
        ON webhook_deliveries(status, created_at);
    CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_event
        ON webhook_deliveries(event_type, created_at);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_webhook_deliveries_object_key ON webhook_deliveries(object_key) WHERE object_key IS NOT NULL;
    CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_check_suite ON webhook_deliveries(check_suite_id, created_at) WHERE check_suite_id IS NOT NULL;


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

    CREATE TABLE IF NOT EXISTS connections (
        id TEXT PRIMARY KEY,
        type TEXT NOT NULL,
        provider TEXT NOT NULL,
        config TEXT,
        status TEXT NOT NULL DEFAULT 'available',
        scope TEXT NOT NULL DEFAULT 'global',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(provider)
    );
    CREATE INDEX IF NOT EXISTS idx_connections_status ON connections(type, status);

    -- Connections rule binder (coordination MVP S3). Ordered rules bind one declared
    -- Event-Model filterable fact + value to a registered destination thread; the
    -- binder owns first-seen owner assignment, then existing artifacts route by
    -- ownership (not by re-running rules). Precedence is explicit; equal-precedence
    -- ties surface a binder-repair artifact, never a silent tiebreak.
    CREATE TABLE IF NOT EXISTS connection_rules (
        id TEXT PRIMARY KEY,
        scope TEXT NOT NULL DEFAULT 'global',
        connector TEXT NOT NULL,
        fact TEXT NOT NULL,
        value TEXT NOT NULL,
        destination_thread_id TEXT NOT NULL,
        precedence INTEGER NOT NULL DEFAULT 100,
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_connection_rules_match
        ON connection_rules(connector, enabled, precedence, created_at);

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
        error TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_self_restart_requests_status
        ON self_restart_requests(status);


    -- A terminal Turn with actionable plan work arms one row per thread.
    -- Reuse the row across generic cycles and known provider-reset deadlines.
    CREATE TABLE IF NOT EXISTS plan_liveness_wakes (
        id TEXT PRIMARY KEY,
        thread_id TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL DEFAULT 'armed',
        deadline TEXT NOT NULL,
        provider_reset_at TEXT,
        provider_reset_turn_id TEXT,
        cycle INTEGER NOT NULL DEFAULT 0,
        last_open_count INTEGER NOT NULL DEFAULT 0,
        last_signal_hash TEXT NOT NULL DEFAULT '',
        last_settled_events INTEGER NOT NULL DEFAULT 0,
        wakes_in_window INTEGER NOT NULL DEFAULT 0,
        window_started_at TEXT,
        armed_at TEXT NOT NULL,
        fired_at TEXT,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_plan_liveness_thread_status
        ON plan_liveness_wakes(thread_id, status);


    CREATE TABLE IF NOT EXISTS artifact_identities (
        id TEXT PRIMARY KEY,
        integration TEXT NOT NULL,
        type TEXT NOT NULL,
        external_id TEXT NOT NULL,
        title TEXT,
        description TEXT,
        path TEXT,
        state TEXT NOT NULL DEFAULT 'active',
        retention TEXT NOT NULL DEFAULT 'durable'
            CHECK (retention IN ('ephemeral', 'durable')),
        metadata TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(integration, type, external_id)
    );
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


    CREATE TABLE IF NOT EXISTS context_change_journal (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        object_kind TEXT NOT NULL,
        object_id TEXT NOT NULL,
        change_kind TEXT NOT NULL,
        summary TEXT NOT NULL,
        details_json TEXT,
        actor_thread_id TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_context_changes_object
        ON context_change_journal(object_kind, object_id, seq);


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

    CREATE TABLE IF NOT EXISTS reviewer_gate_state (
        pr_key       TEXT PRIMARY KEY,
        reviewed_sha TEXT NOT NULL
    );


    CREATE TABLE IF NOT EXISTS doc_comments (
        id TEXT PRIMARY KEY,
        artifact_id TEXT NOT NULL,
        parent_id TEXT REFERENCES doc_comments(id),
        author TEXT NOT NULL,
        author_kind TEXT NOT NULL DEFAULT 'user',
        author_thread_id TEXT,
        body TEXT NOT NULL,
        anchor_text TEXT,
        from_off INTEGER,
        to_off INTEGER,
        anchor_prefix TEXT,
        anchor_suffix TEXT,
        review INTEGER NOT NULL DEFAULT 0,
        resolved INTEGER NOT NULL DEFAULT 0,
        acked INTEGER NOT NULL DEFAULT 0,
        replacement TEXT,
        decision TEXT,
        decided_by TEXT,
        decided_by_thread_id TEXT,
        decided_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_doc_comments_artifact
        ON doc_comments(artifact_id, created_at);

    CREATE TABLE IF NOT EXISTS doc_feedback_batches (
        id TEXT PRIMARY KEY,
        artifact_id TEXT NOT NULL REFERENCES artifact_identities(id),
        author_kind TEXT NOT NULL CHECK (author_kind IN ('agent','user')),
        author_id TEXT NOT NULL,
        recipient_id TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'open'
            CHECK (state IN ('open','delivered','undelivered')),
        opened_at TEXT NOT NULL,
        last_item_at TEXT NOT NULL,
        closes_at TEXT NOT NULL,
        closed_reason TEXT CHECK (
            closed_reason IS NULL OR closed_reason IN (
                'submit','quiet','turn_end','send','leave','idle'
            )
        ),
        item_count INTEGER NOT NULL DEFAULT 0 CHECK (item_count >= 0),
        delivered_node_id TEXT REFERENCES plan_nodes(id),
        delivery_outcome TEXT CHECK (
            delivery_outcome IS NULL OR delivery_outcome IN ('node','ui','empty')
        ),
        undelivered_reason TEXT,
        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
        author_result_notified_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_doc_feedback_batches_open
        ON doc_feedback_batches(author_kind, author_id, artifact_id, recipient_id)
        WHERE state='open';
    CREATE INDEX IF NOT EXISTS idx_doc_feedback_batches_due
        ON doc_feedback_batches(state, closes_at);
    CREATE INDEX IF NOT EXISTS idx_doc_feedback_batches_author
        ON doc_feedback_batches(author_kind, author_id, state, updated_at);

    CREATE TABLE IF NOT EXISTS doc_feedback_batch_items (
        batch_id TEXT NOT NULL REFERENCES doc_feedback_batches(id) ON DELETE CASCADE,
        comment_id TEXT NOT NULL REFERENCES doc_comments(id) ON DELETE CASCADE,
        PRIMARY KEY (batch_id, comment_id)
    );
    CREATE INDEX IF NOT EXISTS idx_doc_feedback_batch_items_comment
        ON doc_feedback_batch_items(comment_id);

    CREATE TABLE IF NOT EXISTS doc_feedback_edit_items (
        batch_id TEXT PRIMARY KEY
            REFERENCES doc_feedback_batches(id) ON DELETE CASCADE,
        actor_display TEXT NOT NULL,
        prior_segments_diff TEXT NOT NULL DEFAULT '',
        baseline_sha256 TEXT NOT NULL,
        baseline_text TEXT NOT NULL,
        latest_sha256 TEXT NOT NULL,
        latest_text TEXT NOT NULL,
        save_count INTEGER NOT NULL CHECK (save_count >= 1),
        first_at TEXT NOT NULL,
        last_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS doc_comment_views (
        user_id TEXT NOT NULL,
        artifact_id TEXT NOT NULL REFERENCES artifact_identities(id) ON DELETE CASCADE,
        viewed_at TEXT NOT NULL,
        PRIMARY KEY (user_id, artifact_id)
    );


    CREATE INDEX IF NOT EXISTS idx_measurement_thread_children
        ON threads(parent_thread_id, user_id) WHERE parent_thread_id IS NOT NULL;

    CREATE INDEX IF NOT EXISTS idx_measurement_human_messages_time
        ON messages(thread_id, created_at, id) WHERE role='user';

    CREATE INDEX IF NOT EXISTS idx_measurement_rejections_time
        ON plan_node_events(created_at, node_id) WHERE event='review_rejected';

    CREATE INDEX IF NOT EXISTS idx_measurement_decisions_thread
        ON artifact_identities(json_extract(metadata, '$.plan_decision.thread_id'))
        WHERE integration='local' AND type='plan-decision' AND json_valid(metadata);


CREATE VIRTUAL TABLE messages_fts USING fts5(
            content, role, content=messages, content_rowid=rowid
        );
CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, content, role)
                VALUES (new.rowid, new.content, new.role);
        END;
CREATE TRIGGER messages_fts_delete AFTER DELETE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, content, role)
                VALUES ('delete', old.rowid, old.content, old.role);
        END;
CREATE TRIGGER messages_fts_update
        AFTER UPDATE OF content, role ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, content, role)
                VALUES ('delete', old.rowid, old.content, old.role);
            INSERT INTO messages_fts(rowid, content, role)
                VALUES (new.rowid, new.content, new.role);
        END;
CREATE VIRTUAL TABLE checkpoints_fts USING fts5(
            raw_result,
            content=checkpoints, content_rowid=generation_order
        );
CREATE TRIGGER checkpoints_fts_insert AFTER INSERT ON checkpoints BEGIN
            INSERT INTO checkpoints_fts(rowid, raw_result)
                VALUES (new.generation_order, new.raw_result);
        END;
CREATE TRIGGER checkpoints_fts_delete AFTER DELETE ON checkpoints BEGIN
            INSERT INTO checkpoints_fts(checkpoints_fts, rowid, raw_result)
                VALUES ('delete', old.generation_order, old.raw_result);
        END;
CREATE TRIGGER checkpoints_fts_update
        AFTER UPDATE OF raw_result ON checkpoints BEGIN
            INSERT INTO checkpoints_fts(checkpoints_fts, rowid, raw_result)
                VALUES ('delete', old.generation_order, old.raw_result);
            INSERT INTO checkpoints_fts(rowid, raw_result)
                VALUES (new.generation_order, new.raw_result);
        END;
CREATE VIRTUAL TABLE checkpoint_entry_search_fts USING fts5(
            search_text,
            content=checkpoint_entry_search,
            content_rowid=rowid
        );
CREATE TRIGGER checkpoint_entry_search_fts_insert
        AFTER INSERT ON checkpoint_entry_search BEGIN
            INSERT INTO checkpoint_entry_search_fts(rowid, search_text)
                VALUES (new.rowid, new.search_text);
        END;
CREATE TRIGGER checkpoint_entry_search_fts_delete
        AFTER DELETE ON checkpoint_entry_search BEGIN
            INSERT INTO checkpoint_entry_search_fts(
                checkpoint_entry_search_fts, rowid, search_text)
                VALUES ('delete', old.rowid, old.search_text);
        END;
CREATE TRIGGER checkpoint_entry_search_fts_update
        AFTER UPDATE OF search_text ON checkpoint_entry_search BEGIN
            INSERT INTO checkpoint_entry_search_fts(
                checkpoint_entry_search_fts, rowid, search_text)
                VALUES ('delete', old.rowid, old.search_text);
            INSERT INTO checkpoint_entry_search_fts(rowid, search_text)
                VALUES (new.rowid, new.search_text);
        END;
PRAGMA user_version=78;
