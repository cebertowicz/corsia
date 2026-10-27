"""Keyless web search using the DDGS library in the backend process."""

import asyncio

from ddgs import DDGS
from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException

from tools.web_search_outcome import (
    FAILED, REASON_NETWORK, REASON_PROVIDER, REASON_RATE_LIMIT, REASON_TIMEOUT, WORKS, SearchOutcome,
)

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
    """Try up to three times while results come back empty, without blocking the event loop.

    The timeout bounds the caller's wait only: a library call already running ends by
    its own timeouts."""
    try:
        async with asyncio.timeout(TIMEOUT_SECONDS):
            for _ in range(3):
                items = await asyncio.to_thread(_search, query, count)
                if items:
                    return SearchOutcome(WORKS, results=items)
        return SearchOutcome(WORKS)
    except (TimeoutError, TimeoutException):
        return SearchOutcome(FAILED, REASON_TIMEOUT)
    except RatelimitException:
        return SearchOutcome(FAILED, REASON_RATE_LIMIT)
    except DDGSException as exc:
        # DDGS wraps a failed request in its base exception, chained from the client error.
        return SearchOutcome(FAILED, REASON_NETWORK if exc.__cause__ else REASON_PROVIDER)
