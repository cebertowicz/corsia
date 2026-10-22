"""Per-project policy tables: repository policy and inference locality.

Repository policy is the branch pin and base-checkout protection;
``infra.project_repo_policy`` reads the rows and ``infra.project_repo_policy_writes``
writes them. Inference policy is the switch that turns cloud models off;
``llm.locality`` reads and writes it.
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

PROJECT_INFERENCE_POLICY_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS project_inference_policies (
        project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
        cloud_models_off INTEGER NOT NULL DEFAULT 0 CHECK (cloud_models_off IN (0,1))
    )
"""
