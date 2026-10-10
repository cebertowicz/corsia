"""Shared lifecycle cleanup for provider runtimes."""

from __future__ import annotations


async def terminate_and_pop(
    thread_id: str,
    *,
    live_registry: dict,
    dead_set: set | None = None,
    extra_pops: list[dict] | None = None,
) -> bool:
    """Terminate first, then remove only the exact handles cleanup proved dead."""
    if dead_set is not None:
        dead_set.add(thread_id)
    live = live_registry.get(thread_id)
    extras = [(reg, reg.get(thread_id)) for reg in extra_pops or ()]
    if live is not None:
        if hasattr(live, "provider_context_untrusted"):
            live.provider_context_untrusted = True
        session = getattr(live, "session", None)
        if session is not None:
            terminate = getattr(session, "terminate", None)
            if terminate is None:
                return False
            await terminate()
        if live_registry.get(thread_id) is not live:
            return False
        live_registry.pop(thread_id, None)
    for registry, expected in extras:
        if registry.get(thread_id) is expected:
            registry.pop(thread_id, None)
    return True
