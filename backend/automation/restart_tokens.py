"""Credential cleanup for settled restart operations."""

from __future__ import annotations


def revoke_lifecycle_token_for_terminal_operation(connection, operation_id: str) -> None:
    """Revoke the operation-labelled service token after the durable settlement."""
    row = connection.execute(
        "SELECT requester_user FROM self_restart_requests WHERE id=?", (operation_id,)
    ).fetchone()
    if row is None or not row["requester_user"]:
        return
    from infra.token_admin import revoke_lifecycle_service_token

    revoke_lifecycle_service_token(row["requester_user"], operation_id, db=connection)
