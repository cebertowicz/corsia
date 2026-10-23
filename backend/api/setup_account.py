"""Create the first account or edit it from setup until setup is finished.

The username is the data owner id and cannot change here. A new password ends every session, so the reply carries
the next one.
"""

from __future__ import annotations

import logging

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from api.setup_requests import AccountUpdateRequest, FirstAccountRequest
from infra import account_validation, accounts, setup_progress
from infra.account_update import update_account
from infra.db_async import run_in_db
from infra.failure_reference import new_error_reference
from infra.time import utc_storage_iso_to_api

logger = logging.getLogger(__name__)


async def apply_create(request: Request) -> dict | Response:
    """Create the first account after the route has checked the transport boundary."""
    try:
        body = FirstAccountRequest.model_validate(await request.json())
    except (ValidationError, ValueError):
        return Response(status_code=422)
    try:
        user_id = await run_in_db(
            accounts.create_first_account, body.username, body.password,
            full_name=body.name, email=body.email or "", first_run_path="app",
        )
    except account_validation.FirstAccountExists:
        return Response(content='{"code":"setup_complete"}', status_code=409, media_type="application/json")
    except account_validation.AccountError as error:
        return JSONResponse(status_code=422, content={"detail": str(error)})
    except Exception:
        # No user field is in this message; the traceback names only the failing step.
        logger.exception("First-account creation failed")
        return JSONResponse(
            status_code=500,
            content={"code": "setup_failed", "detail": "Your account was not created. Try again."},
        )
    token, expires_at = await run_in_db(accounts.issue_token, user_id, kind="session", label="browser")
    user = await run_in_db(accounts.get_user, user_id)
    return {
        "token": token,
        "user": user,
        "expires_at": utc_storage_iso_to_api(expires_at),
    }


async def apply_update(user_id: str, request: Request) -> dict | Response:
    """Apply only the fields the request carries to ``user_id``'s account."""
    try:
        body = AccountUpdateRequest.model_validate(await request.json())
    except (ValidationError, ValueError):
        return Response(status_code=422)
    if await run_in_db(lambda: setup_progress.view()["setup_completed"]):
        return JSONResponse(status_code=409, content={"code": "setup_complete"})
    fields = body.model_fields_set
    try:
        session = await run_in_db(lambda: update_account(
            user_id, full_name=body.name if "name" in fields else None,
            email=(body.email or "") if "email" in fields else None,
            password=body.password if "password" in fields else None))
    except account_validation.AccountError as error:
        return JSONResponse(status_code=422, content={"detail": str(error)})
    except FileNotFoundError:
        # A known cause with a known fix: the name on screen rebuilds the profile, so the client sends it along.
        return JSONResponse(status_code=422, content={"code": "profile_missing"})
    except Exception:
        ref = new_error_reference()
        logger.exception("Account update failed reference=%s", ref)
        return JSONResponse(status_code=500, content={"code": "setup_failed", "detail": "Your account was not changed. Try again.", "ref": ref})
    if not session:
        return {"ok": True}
    token, expires_at = session
    return {"ok": True, "token": token, "user": await run_in_db(accounts.get_user, user_id), "expires_at": utc_storage_iso_to_api(expires_at)}
