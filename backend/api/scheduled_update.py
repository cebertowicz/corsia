"""Nightly trigger for the ordinary Settings Update lifecycle."""

from __future__ import annotations

import os


SCHEDULED_DRAIN_MAX_WAIT_SECONDS = 60


def scheduled_launch_blocker(user_id: str | None) -> str | None:
    """Return the pre-row reason that prevents a scheduled Update launch."""
    from automation.self_restart import in_flight_request
    from infra.accounts import get_user

    if not user_id or get_user(user_id) is None:
        return "scheduled update account no longer exists"
    port = os.environ.get("BACKEND_PORT", "")
    if not port.isdigit():
        return "scheduled update cannot launch: BACKEND_PORT is not a port number"
    if in_flight_request() is not None:
        return "A restart is already in flight."
    return None


async def launch_scheduled_update(
    trigger: str, target_version: str | None, user_id: str | None
) -> str:
    """Run Settings Update as the account that set the active schedule choice."""
    if trigger not in {"tonight", "automatic"}:
        raise ValueError("scheduled update trigger is invalid")
    blocker = scheduled_launch_blocker(user_id)
    if blocker is not None:
        raise RuntimeError(blocker)
    port = os.environ["BACKEND_PORT"]
    from api.system_admin import launch_update
    from infra.accounts import get_user

    user = get_user(user_id)
    if user is None:  # A deletion between the blocker and launch is pre-row.
        raise RuntimeError("scheduled update account no longer exists")
    operation_id, _process = await launch_update(
        user,
        None,
        trigger,
        target_version,
        port=int(port),
        drain_max_wait=SCHEDULED_DRAIN_MAX_WAIT_SECONDS,
    )
    return operation_id
