"""Authenticate and resolve an optional human catalog scope before projection."""
from __future__ import annotations

from fastapi import HTTPException

from infra.auth import authenticate, token_from_request
from infra.database import get_db


def authorize_scope(request, *, project_id=None, thread_id=None, account_id=None):
    if project_id is None and thread_id is None and account_id is None:
        return False
    if request is None:
        raise HTTPException(401)
    user = authenticate(token_from_request(request.query_params.get("token"), request.headers.get("authorization")))
    if user is None:
        raise HTTPException(401)
    db = get_db()
    try:
        if thread_id is not None:
            row = db.execute("SELECT project_id FROM threads WHERE id=? AND user_id=?", (thread_id, user["id"])).fetchone()
            if row is None:
                raise HTTPException(404, "Thread not found")
            if project_id is not None and project_id != row["project_id"]:
                raise HTTPException(409, "Thread project changed")
        elif project_id is not None and db.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None:
            raise HTTPException(404, "Project not found")
        return True
    finally:
        db.close()
