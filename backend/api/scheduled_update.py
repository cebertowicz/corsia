"""Nightly trigger for the ordinary Settings Update lifecycle."""

from __future__ import annotations

import os


async def launch_scheduled_update(
    trigger: str, target_version: str | None, user_id: str | None
) -> str:
    """Run Settings Update as the account that set the active schedule choice."""
    if trigger not in {"tonight", "automatic"}:
        raise ValueError("scheduled update trigger is invalid")
    if not user_id:
        raise RuntimeError("scheduled update has no account owner")
    port = os.environ.get("BACKEND_PORT", "")
    if not port.isdigit():
        raise RuntimeError("scheduled update cannot launch: BACKEND_PORT is not a port number")
    from api.system_admin import launch_update
    from infra.accounts import get_user

    user = get_user(user_id)
    if user is None:
        raise RuntimeError(f"scheduled update account no longer exists: {user_id}")
    operation_id, _process = await launch_update(
        user,
        None,
        trigger,
        target_version,
        port=int(port),
    )
    return operation_id
