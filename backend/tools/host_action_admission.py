"""Refusals for host actions without an active lease."""

import asyncio
from typing import Any

from host_access.refusal_messages import protected_scope_denial
from tools.host_access_errors import HostAccessRequired
from tools.host_action_types import HostActionAuthorization


async def no_lease_authorization(action: Any, ordinary_action: bool, overrides: dict, thread_id: str, *, fence: Any = None) -> Any:
    """Return ordinary admission or explain why a reviewed action cannot run."""
    from tools.host_path_policy import current_protected_host_path_policy

    if ordinary_action:
        return HostActionAuthorization(_parameter_overrides=tuple(overrides.items()))
    if action.tool_name == "bash":
        raise HostAccessRequired(protected_scope_denial(
            "host scope was requested but no host-access lease is active", lease_active=False,
        ))
    if action.lexical_path is not None:
        policy = await asyncio.to_thread(current_protected_host_path_policy, thread_id=thread_id, fence=fence)
        if refusal := policy.fence.refusal(action.lexical_path):
            raise HostAccessRequired(refusal)
    raise HostAccessRequired(protected_scope_denial(
        "this action is outside protected scope", lease_active=False,
    ))
