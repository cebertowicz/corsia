"""Keyless web search using the DDGS library in the backend process."""

import asyncio

from ddgs import DDGS
from ddgs.exceptions import DDGSException, TimeoutException

from tools.web_search_outcome import FAILED, REASON_NETWORK, REASON_PROVIDER, REASON_TIMEOUT, WORKS, SearchOutcome

TIMEOUT_SECONDS = 14


def _search(query: str, count: int) -> list[dict[str, str]]:
    try:
        results = DDGS(timeout=TIMEOUT_SECONDS).text(query, max_results=count)
    except DDGSException as exc:
        if str(exc) == "No results found.":
            return []
        raise
    return [{"title": item.get("title", ""), "url": item.get("href", ""),
             "description": item.get("body", "")} for item in results]


async def keyless_search(query: str, count: int = 5) -> SearchOutcome:
    """Search once without blocking the event loop; an empty result still works.

    The timeout bounds the caller's wait only: a library call already running ends by
    its own timeouts."""
    try:
        async with asyncio.timeout(TIMEOUT_SECONDS):
            items = await asyncio.to_thread(_search, query, count)
        return SearchOutcome(WORKS, results=items)
    except TimeoutError:
        return SearchOutcome(FAILED, REASON_TIMEOUT)
    except DDGSException as exc:
        return SearchOutcome(FAILED, _failure_reason(exc))


def _failure_reason(exc: DDGSException) -> str:
    """Classify by the engine's own error, which DDGS wraps.

    DDGS raises no rate-limit error: an engine answering 429 returns nothing, so it reads as no results."""
    engine = _engine_error(exc)
    if isinstance(exc, TimeoutException) or isinstance(engine, TimeoutException):
        return REASON_TIMEOUT
    return REASON_NETWORK if engine.__cause__ else REASON_PROVIDER


def _engine_error(exc: DDGSException) -> BaseException:
    """The last engine's error: DDGS re-raises it unchained, as its exception's argument.

    A failed request inside an engine is chained from the client error."""
    inner = exc.args[0] if exc.args else None
    return inner if isinstance(inner, BaseException) else exc
