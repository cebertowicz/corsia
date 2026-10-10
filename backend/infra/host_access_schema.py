"""Host-access lease DDL, kept beside the other domain schema modules."""
from __future__ import annotations

HOST_ACCESS_LEASE_DDL = (
    """
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
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_host_access_leases_root_state
        ON host_access_leases(root_thread_id, state)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_host_access_leases_state_expiry
        ON host_access_leases(state, expires_at)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_host_access_leases_one_nonterminal
        ON host_access_leases(root_thread_id)
        WHERE state IN ('reviewing', 'awaiting_user', 'activating', 'active')
    """,
    """
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
    """,
    """
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
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_host_access_action_reviews_lease
        ON host_access_action_reviews(lease_id, created_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_host_access_action_reviews_action
        ON host_access_action_reviews(lease_id, action_hash, created_at)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_host_access_action_reviews_permit
        ON host_access_action_reviews(permit_id)
        WHERE permit_id IS NOT NULL
    """,
)

HOST_ACCESS_LEASE_SCHEMA_SQL = ";\n".join(HOST_ACCESS_LEASE_DDL) + ";"
