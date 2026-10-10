"""Conductor designation routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from infra.auth import authenticate, require_human_user
from infra.conductor import (
    WORKSPACE_SCOPE,
    get_conductor,
    get_conductor_scopes,
    set_conductor,
)
from infra.database import get_db
from infra.db_async import run_in_db, run_read
from infra.onboarding_events import first_run_pending
from infra.queries import get_thread_for_user, thread_is_available
from infra.runtime_truth import read_runtime_truth

router = APIRouter(prefix="/api", tags=["conductor"])


@router.get("/conductor")
async def get_conductor_route(token: str, scope: str = "workspace"):
    user = authenticate(token)
    if not user:
        raise HTTPException(401)

    def _resolve() -> tuple[str | None, dict | None, list[dict], bool]:
        sid = get_conductor(scope)
        if not sid:
            return None, None, [], False
        row = get_thread_for_user(sid, user["id"])
        if not thread_is_available(row):
            return None, None, [], False
        return sid, dict(row), get_conductor_scopes(sid), _first_run_pending(sid)

    def _first_run_pending(sid: str) -> bool:
        # Only the workspace conductor has a first run. It stays pending through
        # a failed first turn, so the landing keeps showing the recovery.
        if scope != WORKSPACE_SCOPE:
            return False
        db = get_db()
        try:
            return first_run_pending(sid, db=db)
        finally:
            db.close()

    thread_id, thread_row, conductor_scopes, pending = await run_read(_resolve)
    if thread_id is None or thread_row is None:
        return {"scope": scope, "thread_id": None, "target": None,
                "first_run_pending": False}

    truth = await read_runtime_truth(
        thread_id, thread_row,
        conductor_scopes=conductor_scopes,
    )
    return {
        "scope": scope,
        "thread_id": thread_id,
        "first_run_pending": pending,
        "target": {
            "thread_id": thread_id,
            "title": thread_row.get("title"),
            "bootloader_files": thread_row.get("bootloader_files"),
            # The thread list omits the conductor; this is the server time
            # that orders its model against a later committed switch.
            "updated_at": thread_row.get("updated_at"),
            **truth,
        },
    }


@router.put("/conductor")
async def put_conductor_route(token: str, body: dict, scope: str = "workspace"):
    user = authenticate(token)
    if not user:
        raise HTTPException(401)
    require_human_user(user)
    thread_id = body.get("thread_id")  # null clears the designation

    def _set() -> str:
        if thread_id is not None and not get_thread_for_user(thread_id, user["id"]):
            return "no_session"
        return "ok" if set_conductor(scope, thread_id) else "no_scope"

    result = await run_in_db(_set)
    if result == "no_session":
        raise HTTPException(404, "Thread not found")
    if result == "no_scope":
        raise HTTPException(400, f"unknown conductor scope: {scope!r}")
    return {"ok": True, "scope": scope, "thread_id": thread_id}
