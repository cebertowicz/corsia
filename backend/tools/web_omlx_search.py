"""Keyless web search through an already-running managed oMLX server."""

from __future__ import annotations

from typing import Any

import httpx

from tools.web_search_outcome import (
    FAILED,
    REASON_NETWORK,
    REASON_PROVIDER,
    REASON_TIMEOUT,
    WORKS,
    SearchOutcome,
    classify_status,
)

# Three empty-result tries plus two one-second waits must finish within ~45 seconds.
ATTEMPT_TIMEOUT_SECONDS = 14.0


async def omlx_search_target() -> tuple[str, dict[str, str]] | None:
    """Return the public search endpoint for the already-live managed server.

    This only reads CORSIA's managed-server registration and live endpoint. It
    does not start oMLX or probe a stale endpoint.
    """
    from infra.db_async import run_in_db
    from llm.local_sources import corsia_omlx_server
    from llm.network_endpoints import build_api_base
    from llm.providers.omlx_public_auth import public_headers

    server = await run_in_db(corsia_omlx_server)
    if server is None or server.get("status") != "online":
        return None
    api_base = await run_in_db(build_api_base, server["id"])
    if api_base is None:
        return None
    headers = await public_headers({"server_id": server["id"]}, url=api_base)
    return f"{api_base.rstrip('/')}/web/search", headers


def _result_items(body: dict[str, Any]) -> list[dict[str, str]] | None:
    """Normalize oMLX's public result shape to the existing Brave result shape."""
    if body.get("ok") is not True:
        return None
    raw_results = body.get("results")
    if not isinstance(raw_results, list):
        return None
    items: list[dict[str, str]] = []
    for result in raw_results:
        if not isinstance(result, dict):
            continue
        title = result.get("title")
        item = {"title": title if isinstance(title, str) else ""}
        url = result.get("url") or result.get("href")
        if isinstance(url, str):
            item["url"] = url
        snippet = result.get("snippet")
        if isinstance(snippet, str):
            item["description"] = snippet
        items.append(item)
    return items


async def run_omlx_search(query: str) -> SearchOutcome | None:
    """Search the managed oMLX server once, returning no target when it is absent."""
    try:
        target = await omlx_search_target()
    except Exception:
        return SearchOutcome(FAILED, REASON_PROVIDER)
    if target is None:
        return None
    url, headers = target
    try:
        async with httpx.AsyncClient(timeout=ATTEMPT_TIMEOUT_SECONDS) as client:
            response = await client.post(url, json={"query": query}, headers=headers)
    except httpx.TimeoutException:
        return SearchOutcome(FAILED, REASON_TIMEOUT)
    except httpx.HTTPError:
        return SearchOutcome(FAILED, REASON_NETWORK)
    except Exception:
        return SearchOutcome(FAILED, REASON_PROVIDER)
    try:
        body = response.json()
    except ValueError:
        body = None
    failure = classify_status(response.status_code, body)
    if failure is not None:
        return failure
    if not isinstance(body, dict):
        return SearchOutcome(FAILED, REASON_PROVIDER)
    items = _result_items(body)
    if items is None:
        return SearchOutcome(FAILED, REASON_PROVIDER)
    return SearchOutcome(WORKS, results=items)
