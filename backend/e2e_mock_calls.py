"""Mock model calls the seeded queue does not serve: shown to the test, never absorbed."""

from __future__ import annotations

from loguru import logger

LIMIT = 100
unmatched: list[dict] = []


def calling_thread_id() -> str | None:
    from infra import state

    return state._current_thread_id.get(None)


def reset() -> None:
    unmatched.clear()


def record(thread_id: str | None, stream: bool | None) -> None:
    """A call from a thread no seeded reply names gets the default reply; log it and list it for the test."""
    logger.warning("E2E mock: model call from thread {} (stream={}) matches no seeded reply", thread_id, stream)
    if len(unmatched) < LIMIT:
        unmatched.append({"thread_id": thread_id, "stream": stream})
