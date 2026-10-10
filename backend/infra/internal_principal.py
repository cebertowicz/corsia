"""Revocable, task-local authentication for an in-process conductor request.

This is never a network credential. An inherited background context stops
being usable as soon as the originating request finishes.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator


@dataclass
class _Principal:
    user: dict
    active: bool = True


_current: ContextVar[_Principal | None] = ContextVar("corsia_internal_principal", default=None)


def current_internal_user() -> dict | None:
    principal = _current.get()
    if principal is None or not principal.active:
        return None
    return dict(principal.user)


@contextmanager
def internal_request(user_id: str, thread_id: str) -> Iterator[None]:
    """Authenticate only the bounded ASGI call, never a subsequent task.

    api/system.py treats ``internal`` as conductor-only (it may set the host-access
    reviewer model), so any new caller must pass the conductor-capability check first.
    """
    principal = _Principal({"id": user_id, "thread_id": thread_id, "token_kind": "internal"})
    binding = _current.set(principal)
    try:
        yield
    finally:
        principal.active = False
        _current.reset(binding)
