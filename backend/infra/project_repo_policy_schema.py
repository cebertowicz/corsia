"""Per-project repository policy: branch pin and base-checkout protection.

``infra.project_repo_policy`` reads the rows and
``infra.project_repo_policy_writes`` writes them.
"""

from __future__ import annotations

PROJECT_REPO_POLICY_TABLE_SQL = """
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
"""
