"""Refusals for host actions without an active lease."""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from host_access.refusal_messages import protected_scope_denial
from tools.host_access_errors import HostAccessRequired
from tools.host_action_types import HostActionAuthorization


_no_host_lease: ContextVar[bool] = ContextVar('no_host_lease', default=False)


def host_lease_disabled() -> bool:
    return _no_host_lease.get()


@contextmanager
def without_host_lease() -> Iterator[None]:
    """Keep a disposable caller from using its owning thread's host lease."""
    token = _no_host_lease.set(True)
    try:
        yield
    finally:
        _no_host_lease.reset(token)


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
