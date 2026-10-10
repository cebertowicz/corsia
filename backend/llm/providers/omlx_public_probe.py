"""Authenticated probes for oMLX public status surfaces."""

from __future__ import annotations

import asyncio

import httpx

from llm.network_endpoints import mgmt_status_url, probe_endpoint
from llm.providers.omlx_public_auth import OmlxPublicAuthError, public_headers


async def probe_registered_status(
    client: httpx.AsyncClient, server_id: str, host: str, port: int,
) -> tuple[str, int, dict | None] | None:
    """Probe one registered oMLX status endpoint with its current public key."""
    url = mgmt_status_url(host, port)
    try:
        headers = await public_headers({"server_id": server_id}, url=url)
    except OmlxPublicAuthError:
        return None
    return await probe_endpoint(
        client, host=host, url=url, headers=headers, external=False,
    )


async def probe_registered_statuses(
    client: httpx.AsyncClient, server_id: str, endpoints: list[dict], port: int,
) -> list[tuple[str, int, dict | None] | None | BaseException]:
    """Probe every registered managed address for freshness recovery."""
    return await asyncio.gather(
        *(probe_registered_status(client, server_id, endpoint["host"], port) for endpoint in endpoints),
        return_exceptions=True,
    )
