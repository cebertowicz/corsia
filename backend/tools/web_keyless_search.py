"""Keyless web search using the DDGS library in the backend process."""

import asyncio
import logging

from ddgs import DDGS
from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException

from tools.web_search_outcome import (
    FAILED, REASON_NETWORK, REASON_PROVIDER, REASON_RATE_LIMIT, REASON_TIMEOUT, WORKS, SearchOutcome,
)

TIMEOUT_SECONDS = 14
log = logging.getLogger(__name__)


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
    """Retry empty results at most three times, without blocking the event loop."""
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
    except Exception as exc:
        # Anything else is not the network's fault. Only its type is logged: its text can carry the query.
        log.warning("keyless search failed unexpectedly: %s", type(exc).__name__)
        return SearchOutcome(FAILED, REASON_PROVIDER)
