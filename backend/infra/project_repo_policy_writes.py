"""SQL-only repository policy registration and removal."""

from __future__ import annotations

from typing import TYPE_CHECKING

from infra.database import write_transaction

if TYPE_CHECKING:
    from infra.project_repo_policy import RepoPolicy


def register_prepared_repo_policy(policy: RepoPolicy, *, db=None) -> RepoPolicy:
    """Persist prepared registration values without filesystem work."""
    with write_transaction(db, scope="register_repo_policy") as connection:
        connection.execute(
            """INSERT INTO project_repo_policies
               (project_id, repo_name, repo_path, pinned_branch, protect_base_checkout)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(project_id, repo_name) DO UPDATE SET
               repo_path = excluded.repo_path, pinned_branch = excluded.pinned_branch,
               protect_base_checkout = excluded.protect_base_checkout""",
            (
                policy.project_id, policy.repo_name, str(policy.path),
                policy.pinned_branch, int(policy.protect_base_checkout),
            ),
        )
    return policy


def clear_repo_policy(project_id: str, repo_name: str, *, db=None) -> None:
    """Remove a registration without committing caller-owned transaction state."""
    with write_transaction(db, scope="clear_repo_policy") as connection:
        connection.execute(
            "DELETE FROM project_repo_policies WHERE project_id = ? AND repo_name = ?",
            (project_id, repo_name),
        )
