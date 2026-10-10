"""One reading of a web search: works, key refused, or failed with a reason class.

``web_search``, Settings › Keys' Check button and the conductor's checks all
share this, so a refused key is never mistaken for a network or quota failure.

- **works:** results, or none.
- **key refused:** the provider answers 401 or 403, or its error body's code
  says the key or subscription is invalid, whatever the status.
- **failed:** network, timeout, rate limit or quota (429), or provider error
  (every other status, 4xx or 5xx).

Nothing here keeps or returns exception text or the provider's error text:
either could carry the key, and neither tells the caller more than the class.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import httpx

SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
#: The fixed query Check, turn 1's detection and the conductor's proof use.
CHECK_QUERY = "CORSIA"
TIMEOUT_SECONDS = 10.0

WORKS = "works"
KEY_REFUSED = "key_refused"
FAILED = "failed"

REASON_NETWORK = "network"
REASON_TIMEOUT = "timeout"
REASON_RATE_LIMIT = "rate limit or quota"
REASON_PROVIDER = "provider error"

#: An error code naming a bad key or subscription, e.g. SUBSCRIPTION_TOKEN_INVALID.
_KEY_ERROR_CODE = re.compile(
    r"(?:TOKEN|KEY|SUBSCRIPTION)[A-Z_]*_(?:INVALID|NOT_FOUND|EXPIRED|REVOKED|INACTIVE)"
    r"|INVALID_[A-Z_]*(?:TOKEN|KEY|SUBSCRIPTION)",
)


@dataclass(frozen=True)
class SearchOutcome:
    """What one search came to. ``results`` is empty unless it worked."""

    kind: str
    reason: str | None = None
    results: list[dict[str, Any]] = field(default_factory=list)


def _error_code(body: object) -> str:
    if not isinstance(body, dict):
        return ""
    error = body.get("error")
    code = error.get("code") if isinstance(error, dict) else body.get("code")
    return code.upper() if isinstance(code, str) else ""


def classify_status(status: int, body: object) -> SearchOutcome | None:
    """Classify a provider answer; None means the status was a success."""
    if _KEY_ERROR_CODE.search(_error_code(body)) or status in (401, 403):
        return SearchOutcome(KEY_REFUSED)
    if status == 429:
        return SearchOutcome(FAILED, REASON_RATE_LIMIT)
    if status >= 400:
        return SearchOutcome(FAILED, REASON_PROVIDER)
    return None


async def run_search(api_key: str, query: str, count: int) -> SearchOutcome:
    """Run one search with ``api_key`` in its header only, and classify it."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                SEARCH_URL,
                params={"q": query, "count": count},
                headers={"Accept": "application/json", "X-Subscription-Token": api_key},
                timeout=TIMEOUT_SECONDS,
            )
    except httpx.TimeoutException:
        return SearchOutcome(FAILED, REASON_TIMEOUT)
    except httpx.HTTPError:
        return SearchOutcome(FAILED, REASON_NETWORK)
    except Exception:
        # Anything else is not the network's fault; its text is never kept.
        return SearchOutcome(FAILED, REASON_PROVIDER)
    try:
        body = response.json()
    except ValueError:
        body = None
    refused = classify_status(response.status_code, body)
    if refused is not None:
        return refused
    if not isinstance(body, dict):
        return SearchOutcome(FAILED, REASON_PROVIDER)
    web = body.get("web")
    results = web.get("results") if isinstance(web, dict) else None
    items = [item for item in results or [] if isinstance(item, dict)] if isinstance(results, list) else []
    return SearchOutcome(WORKS, results=items)


def outcome_line(outcome: SearchOutcome) -> str:
    """The fixed first line a refusal or failure starts with."""
    if outcome.kind == KEY_REFUSED:
        return "Web search: key refused."
    return f"Web search failed: {outcome.reason}."
