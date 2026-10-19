"""Structured-plan schema: plans, nodes, admission, review, and cost.

One module per durable domain, composed into `SCHEMA_SQL` by the schema owner;
Plans' DDL lives here and in the three Plans modules this one composes.
"""

from __future__ import annotations

from infra.plan_aggregates_schema import (
    NODE_AGGREGATE_COLUMNS, NODE_AGGREGATE_INDEXES_SQL, NODE_UPDATE_TRIGGER_SQL,
    PLAN_AGGREGATE_COLUMNS, TOUCH_INDEXES_SQL, column_ddl,
)
from infra.plan_events_schema import PLAN_EVENTS_SCHEMA_SQL
from infra.plan_pane_views_schema import PLAN_PANE_VIEWS_SCHEMA_SQL
from infra.plan_status_schema import PLAN_STATUS_DIRTY_SCHEMA_SQL
from infra.unattributed_dispatch_schema import UNATTRIBUTED_DISPATCH_SCHEMA_SQL

#: A hold's identity never changes and it is cleared once. Its own constant so
#: a schema step that must rewrite one recorded fact re-creates this guard
#: from the one definition (migration_58_to_59).
HOLD_UPDATE_GUARD_SQL = """
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
"""

STRUCTURED_PLAN_SCHEMA_SQL = f"""
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
        archived_at TEXT{column_ddl(PLAN_AGGREGATE_COLUMNS)},
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
        size TEXT CHECK (
            size IS NULL OR size IN ('XS', 'S', 'M', 'L', 'XL', 'XXL', 'unknown')
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
        archived_at TEXT{column_ddl(NODE_AGGREGATE_COLUMNS)}
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
    {NODE_AGGREGATE_INDEXES_SQL}

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
    -- Deferrals belong to open asks; watches alone end them, so no condition is stored here.
    CREATE TABLE IF NOT EXISTS plan_ask_deferrals (
        hold_id TEXT PRIMARY KEY REFERENCES plan_node_holds(id),
        deferred_at TEXT NOT NULL
    );
""" + HOLD_UPDATE_GUARD_SQL + PLAN_STATUS_DIRTY_SCHEMA_SQL + f"""
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
    {NODE_UPDATE_TRIGGER_SQL}
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
""" + PLAN_EVENTS_SCHEMA_SQL + UNATTRIBUTED_DISPATCH_SCHEMA_SQL + PLAN_PANE_VIEWS_SCHEMA_SQL + TOUCH_INDEXES_SQL
