"""Observe-only health sweep for one External provider.

Every collaborator is reached through ``network_discovery`` at call time, so
the sweep reads and writes exactly what the rest of the sweep loop does.
"""

from __future__ import annotations

import time

import httpx

from infra.db_async import run_in_db
from llm.external_declared_models import LIST_UNSUPPORTED, declared_model_ids
from llm.network_endpoints import external_probe_url
from llm.server_observation import record_reachable, record_unreachable


async def sweep_external(client: httpx.AsyncClient, server: dict) -> None:
    """Probe ``/models``; online on a list, or on no list when ids are declared."""
    from llm import network_discovery as nd
    from llm.external_provider import auth_headers

    sid = server["id"]
    port = server["port"]
    dialect = server.get("dialect") or "openai"
    stored = await run_in_db(nd.read_server_metadata, sid)
    meta = stored or {}
    declared = declared_model_ids(meta)
    headers = auth_headers(dialect, meta.get("api_key"))

    online = False
    fastest_latency: int | None = None
    observed: set[str] = set()

    for ep in await run_in_db(nd.get_endpoints, sid):
        # The same reading of the stored base that dispatch and the freshness
        # guard use: a base that cannot be dispatched to is not swept as online.
        probe_url = external_probe_url(stored, dialect=dialect, host=ep["host"], port=port)
        if probe_url is None:
            continue
        try:
            t0 = time.perf_counter()
            resp = await client.get(probe_url, headers=headers)
            dt = int((time.perf_counter() - t0) * 1000)
            unlisted = bool(declared) and resp.status_code in LIST_UNSUPPORTED
            if resp.status_code == 200 or unlisted:
                await run_in_db(
                    nd.upsert_endpoint, sid, ep["host"], ok=True, latency_ms=dt,
                )
                online = True
                if fastest_latency is None or dt < fastest_latency:
                    fastest_latency = dt
                for m in ([] if unlisted else (resp.json().get("data") or [])):
                    mid = m.get("id")
                    if isinstance(mid, str) and mid:
                        observed.add(mid)
        except (httpx.HTTPError, OSError, ValueError):
            pass

    if online:
        await record_reachable(server, loaded=observed | set(declared), latency_ms=fastest_latency)
    else:
        await record_unreachable(server)
