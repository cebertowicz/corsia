"""Event routing exports."""

from __future__ import annotations

__all__ = ["route_event", "RouteResult", "RouteOutcome"]


def __getattr__(name: str):
    """Load router exports only when a caller asks for them."""
    if name not in __all__:
        raise AttributeError(name)
    from events import router

    return getattr(router, name)
