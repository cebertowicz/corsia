"""Schema fragment for the Markdown plan-import ledger and its issues."""

PLAN_MARKDOWN_IMPORT_SCHEMA_SQL = """
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
"""
