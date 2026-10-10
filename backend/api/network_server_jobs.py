"""Network-server job and cache proxy routes."""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, HTTPException

from infra.db_async import run_in_db
from llm import network_discovery as nd
from llm import network_endpoints as ne

router = APIRouter()
log = logging.getLogger(__name__)


@router.get("/{server_id}/cache/status")
async def cache_status(server_id: str):
    server = await run_in_db(nd.get_server, server_id)
    if not server:
        raise HTTPException(404, detail="Server not found")
    mgmt_base = await run_in_db(ne.build_mgmt_base, server_id)
    if not mgmt_base:
        return {"caches": []}
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{mgmt_base}/cache/status")
            if r.status_code == 200:
                return r.json()
    except (httpx.HTTPError, ConnectionError, TimeoutError) as exc:
        log.debug("cache_status proxy failed: %s", exc)
    return {"caches": []}


@router.get("/{server_id}/jobs/active")
async def list_active_jobs(server_id: str, kind: str = "", target: str = ""):
    server = await run_in_db(nd.get_server, server_id)
    if not server:
        raise HTTPException(404, detail="Server not found")
    mgmt_base = await run_in_db(ne.build_mgmt_base, server_id)
    if not mgmt_base:
        return {"jobs": []}
    params: dict = {}
    if kind:
        params["kind"] = kind
    if target:
        params["target"] = target
    try:
        async with httpx.AsyncClient(timeout=4) as client:
            r = await client.get(f"{mgmt_base}/jobs/active", params=params)
            if r.status_code == 200:
                return r.json()
    except (httpx.HTTPError, ConnectionError, TimeoutError) as exc:
        log.debug("list_active_jobs proxy failed: %s", exc)
    return {"jobs": []}


@router.get("/{server_id}/jobs/{job_id}")
async def get_job(server_id: str, job_id: str):
    server = await run_in_db(nd.get_server, server_id)
    if not server:
        raise HTTPException(404, detail="Server not found")
    mgmt_base = await run_in_db(ne.build_mgmt_base, server_id)
    if not mgmt_base:
        raise HTTPException(503, detail="Server has no reachable endpoint")
    try:
        async with httpx.AsyncClient(timeout=4) as client:
            r = await client.get(f"{mgmt_base}/jobs/{job_id}")
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                raise HTTPException(404, detail="Job not found or expired")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=str(e))
    raise HTTPException(502, detail="Daemon returned unexpected status")


@router.delete("/{server_id}/jobs/{job_id}")
async def cancel_job(server_id: str, job_id: str):
    server = await run_in_db(nd.get_server, server_id)
    if not server:
        raise HTTPException(404, detail="Server not found")
    mgmt_base = await run_in_db(ne.build_mgmt_base, server_id)
    if not mgmt_base:
        raise HTTPException(503, detail="Server has no reachable endpoint")
    try:
        async with httpx.AsyncClient(timeout=4) as client:
            r = await client.delete(f"{mgmt_base}/jobs/{job_id}")
            if r.status_code in (200, 202):
                if r.headers.get("content-type", "").startswith("application/json"):
                    return r.json()
                return {"ok": True}
            if r.status_code == 404:
                raise HTTPException(404, detail="Job not found or expired")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=str(e))
    raise HTTPException(502, detail="Daemon returned unexpected status")
