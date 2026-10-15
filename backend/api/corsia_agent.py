"""CORSIA agent designation routes (the workspace scope's one agent)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from api.conductor import read_designation, write_designation
from infra.auth import authenticate
from infra.conductor import WORKSPACE_SCOPE

router = APIRouter(prefix="/api", tags=["corsia-agent"])


@router.get("/corsia-agent")
async def get_corsia_agent_route(token: str):
    user = authenticate(token)
    if not user:
        raise HTTPException(401)
    return await read_designation(user, WORKSPACE_SCOPE)


@router.put("/corsia-agent")
async def put_corsia_agent_route(token: str, body: dict):
    user = authenticate(token)
    if not user:
        raise HTTPException(401)
    return await write_designation(user, WORKSPACE_SCOPE, body.get("thread_id"))  # null clears
