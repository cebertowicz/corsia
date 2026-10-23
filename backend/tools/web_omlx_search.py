"""Keyless web search through an already-running managed oMLX server."""

from __future__ import annotations

from typing import Any

import httpx

from tools.web_search_outcome import (
    FAILED,
    REASON_NETWORK,
    REASON_PROVIDER,
    REASON_RATE_LIMIT,
    REASON_TIMEOUT,
    WORKS,
    SearchOutcome,
)

NO_SERVER = "no_server"
SERVER_STOPPED = "server_stopped"
TIMEOUT_SECONDS = 14.0


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


async def _search_target() -> tuple[str, dict[str, str]] | SearchOutcome | str:
    """Resolve the already-live managed server without starting or probing it."""
    from infra.db_async import run_in_db
    from llm.local_sources import corsia_omlx_server
    from llm.network_endpoints import build_api_base
    from llm.providers.omlx_public_auth import OmlxPublicAuthError, public_headers

    server = await run_in_db(corsia_omlx_server)
    if server is None:
        return NO_SERVER
    if server.get("status") != "online":
        return SERVER_STOPPED
    api_base = await run_in_db(build_api_base, server["id"])
    if api_base is None:
        return SERVER_STOPPED
    try:
        headers = await public_headers({"server_id": server["id"]}, url=api_base)
    except OmlxPublicAuthError:
        return SearchOutcome(FAILED, REASON_PROVIDER)
    return f"{api_base.rstrip('/')}/web/search", headers


async def _search_once(url: str, headers: dict[str, str], query: str) -> SearchOutcome:
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.post(url, json={"query": query}, headers=headers)
    except httpx.TimeoutException:
        return SearchOutcome(FAILED, REASON_TIMEOUT)
    except httpx.HTTPError:
        return SearchOutcome(FAILED, REASON_NETWORK)
    try:
        body = response.json()
    except ValueError:
        body = None
    # No key is in play here, so no answer from oMLX is ever a refused key.
    if response.status_code == 429:
        return SearchOutcome(FAILED, REASON_RATE_LIMIT)
    if response.status_code >= 400 or not isinstance(body, dict):
        return SearchOutcome(FAILED, REASON_PROVIDER)
    items = _result_items(body)
    if items is None:
        return SearchOutcome(FAILED, REASON_PROVIDER)
    return SearchOutcome(WORKS, results=items)


async def keyless_search(query: str) -> SearchOutcome | str:
    """Search once through the managed oMLX server, or name why it is unavailable."""
    target = await _search_target()
    if not isinstance(target, tuple):
        return target
    url, headers = target
    return await _search_once(url, headers, query)
