"""Nightly trigger for the ordinary Settings Update lifecycle."""

from __future__ import annotations

import os
from collections.abc import Callable


SCHEDULED_DRAIN_MAX_WAIT_SECONDS = 60


async def launch_scheduled_update(
    trigger: str, target_version: str | None, schedule: dict
) -> str | None:
    """Run Settings Update as the account that owns this exact schedule snapshot."""
    if trigger not in {"tonight", "automatic"}:
        raise ValueError("scheduled update trigger is invalid")

    from api.system_admin import launch_update
    from automation.update_schedule import claim_tonight
    from infra.accounts import get_user

    user = get_user(schedule.get("owner_id"))
    if user is None:
        raise RuntimeError("scheduled update account no longer exists")
    port = os.environ.get("BACKEND_PORT", "")
    if not port.isdigit():
        raise RuntimeError("scheduled update cannot launch: BACKEND_PORT is not a port number")

    claim_hook: Callable[[], bool] | None = None
    if trigger == "tonight":
        claim_hook = lambda: claim_tonight(schedule)
    launched = await launch_update(
        user,
        None,
        trigger,
        target_version,
        port=int(port),
        drain_max_wait=SCHEDULED_DRAIN_MAX_WAIT_SECONDS,
        claim_hook=claim_hook,
    )
    return launched[0] if launched is not None else None
