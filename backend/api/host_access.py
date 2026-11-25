"""Authenticated HTTP surface for host-access lease decisions."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, model_validator

from host_access.decision_capability import (
    INTERACTIVE_UI_HEADER,
    USER_DECISION_HEADER,
    attach_user_decision_capability,
    verify_user_decision_capability,
)
from host_access.supervisor import (
    approve_and_activate,
    get_host_access_lease,
    list_host_access_leases,
    reject_pending_lease,
    revoke_active_lease,
)
from infra.auth import require_human_user, require_user
from infra.host_access_leases import (
    LeaseConflictError,
    LeaseNotFoundError,
)
from infra.time import utc_storage_iso_to_api


router = APIRouter(prefix="/api/host-access/leases", tags=["host-access"])

_PUBLIC_TIMESTAMP_FIELDS = (
    "application_reviewed_at",
    "user_decided_at",
    "activated_at",
    "activation_wake_delivered_at",
    "expires_at",
    "created_at",
    "updated_at",
)


class LeaseMutation(BaseModel):
    """Optimistic-concurrency binding for one user decision."""

    model_config = ConfigDict(extra="forbid")

    revision: StrictInt
    application_hash: str


class ScriptMutation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    application_hash: str


class FillApproval(LeaseMutation):
    """The person's yes to a saved-login fill: the duration they picked, or until they revoke it; exactly one."""

    duration_seconds: StrictInt | None = None
    until_revoked: StrictBool = False

    @model_validator(mode="after")
    def _one_choice(self) -> "FillApproval":
        if (self.duration_seconds is None) != self.until_revoked:
            raise ValueError("Pick one: a duration or until revoked.")
        return self


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, LeaseNotFoundError):
        return HTTPException(404, "Host-access lease not found")
    if isinstance(exc, LeaseConflictError):
        return HTTPException(409, str(exc))
    return HTTPException(400, str(exc))


def _serialize_lease(row: dict) -> dict:
    """Convert domain-naive UTC timestamps only at the public API boundary."""
    result = dict(row)
    for field in _PUBLIC_TIMESTAMP_FIELDS:
        if field in result or result.get("kind") not in ("script", "fill"):
            result[field] = utc_storage_iso_to_api(result.get(field))
    return result


async def _decide_script(schedule_id: str, body: ScriptMutation, user: dict,
                         capability: str | None, approve: bool):
    from automation.script_decisions import decide
    from events.delivery import deliver_to_thread
    from infra.db_async import run_in_db

    if not verify_user_decision_capability(
        capability, {"id": schedule_id, "application_hash": body.application_hash},
        user_id=user["id"], operation="approve" if approve else "reject",
    ):
        raise HTTPException(403, "A current interactive user decision is required")
    try:
        thread_id, state = await run_in_db(decide, schedule_id, user["id"], body.application_hash, approve)
    except LookupError as exc:
        raise HTTPException(404, "Script request not found") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    from host_access.change_nudge import changed_message
    from infra import state as runtime_state
    runtime_state.broadcast_to_user(user["id"], changed_message({"id": schedule_id, "root_thread_id": thread_id}))
    await deliver_to_thread(thread_id, f"script_access.{state}", f"Authenticated script {state}.",
                            {"schedule_id": schedule_id}, delivery_id=f"script-access:{schedule_id}:{state}")
    return {"id": schedule_id, "state": state}


@router.post("/scripts/{schedule_id}/approve")
async def approve_script(schedule_id: str, body: ScriptMutation,
                         user: dict = Depends(require_human_user),
                         capability: str | None = Header(None, alias=USER_DECISION_HEADER)):
    return await _decide_script(schedule_id, body, user, capability, True)


@router.post("/scripts/{schedule_id}/reject")
async def reject_script(schedule_id: str, body: ScriptMutation,
                        user: dict = Depends(require_human_user),
                        capability: str | None = Header(None, alias=USER_DECISION_HEADER)):
    return await _decide_script(schedule_id, body, user, capability, False)


async def _decide_fill(operation_name: str, approval_id: str, body: LeaseMutation, user: dict,
                       capability: str | None, **extra) -> dict:
    """A person's decision on a saved-login fill. A fill approval is not a lease; it shares only this surface."""
    from infra import saved_login_fills as fills

    operation = {"approve": fills.approve_fill, "reject": fills.deny_fill, "revoke": fills.revoke_fill}[operation_name]

    def decide() -> dict:
        fills.get(approval_id, user_id=user["id"])
        if not verify_user_decision_capability(
            capability, {"id": approval_id, "revision": body.revision, "application_hash": body.application_hash},
            user_id=user["id"], operation=operation_name,
        ):
            raise HTTPException(403, "A current interactive user decision is required")
        return operation(
            approval_id, user_id=user["id"], revision=body.revision, application_hash=body.application_hash, **extra,
        )

    try:
        # A worker thread: the decision records a product event, a blocking write.
        row = await asyncio.to_thread(decide)
    except fills.FillApprovalNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except fills.FillDurationError as exc:
        raise HTTPException(400, str(exc)) from exc
    except fills.FillApprovalError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _serialize_lease(row)


@router.post("/fills/{approval_id}/approve")
async def approve_fill(approval_id: str, body: FillApproval,
                       user: dict = Depends(require_human_user),
                       capability: str | None = Header(None, alias=USER_DECISION_HEADER)):
    return await _decide_fill("approve", approval_id, body, user, capability, duration_seconds=body.duration_seconds,
                              until_revoked=body.until_revoked)


@router.post("/fills/{approval_id}/reject")
async def deny_fill(approval_id: str, body: LeaseMutation,
                    user: dict = Depends(require_human_user),
                    capability: str | None = Header(None, alias=USER_DECISION_HEADER)):
    return await _decide_fill("reject", approval_id, body, user, capability)


@router.post("/fills/{approval_id}/revoke")
async def revoke_fill(approval_id: str, body: LeaseMutation,
                      user: dict = Depends(require_human_user),
                      capability: str | None = Header(None, alias=USER_DECISION_HEADER)):
    return await _decide_fill("revoke", approval_id, body, user, capability)


@router.get("")
async def leases(
    root_thread_id: str | None = None,
    nonterminal_only: bool = False,
    user: dict = Depends(require_user),
    interactive_ui: str | None = Header(
        None,
        alias=INTERACTIVE_UI_HEADER,
    ),
):
    rows = await list_host_access_leases(
        user["id"],
        root_thread_id=root_thread_id,
        nonterminal_only=nonterminal_only,
    )
    from automation.script_decisions import pending
    from infra.db_async import run_in_db
    rows.extend(await run_in_db(pending, user["id"], root_thread_id))
    from infra.saved_login_fills import listed
    rows.extend(await asyncio.to_thread(listed, user["id"], root_thread_id))
    if interactive_ui == "1":
        require_human_user(user)
        rows = [
            attach_user_decision_capability(row, user_id=user["id"]) for row in rows
        ]
    return {"leases": [_serialize_lease(row) for row in rows]}


@router.get("/{lease_id}")
async def lease(
    lease_id: str,
    user: dict = Depends(require_user),
    interactive_ui: str | None = Header(
        None,
        alias=INTERACTIVE_UI_HEADER,
    ),
):
    try:
        row = await get_host_access_lease(lease_id, user_id=user["id"])
        if interactive_ui == "1":
            require_human_user(user)
            row = attach_user_decision_capability(
                row,
                user_id=user["id"],
            )
        return _serialize_lease(row)
    except (LeaseNotFoundError, LeaseConflictError) as exc:
        raise _http_error(exc) from exc


async def _decide(
    operation,
    operation_name: str,
    lease_id: str,
    body: LeaseMutation,
    user: dict,
    capability: str | None,
):
    try:
        await get_host_access_lease(lease_id, user_id=user["id"])
        if not verify_user_decision_capability(
            capability,
            {
                "id": lease_id,
                "revision": body.revision,
                "application_hash": body.application_hash,
            },
            user_id=user["id"],
            operation=operation_name,
        ):
            raise HTTPException(
                403,
                "A current interactive user decision is required",
            )
        row = await operation(
            lease_id,
            user_id=user["id"],
            expected_revision=body.revision,
            application_hash=body.application_hash,
        )
        return _serialize_lease(row)
    except (LeaseNotFoundError, LeaseConflictError) as exc:
        raise _http_error(exc) from exc


@router.post("/{lease_id}/approve")
async def approve(
    lease_id: str,
    body: LeaseMutation,
    user: dict = Depends(require_human_user),
    capability: str | None = Header(None, alias=USER_DECISION_HEADER),
):
    return await _decide(
        approve_and_activate,
        "approve",
        lease_id,
        body,
        user,
        capability,
    )


@router.post("/{lease_id}/reject")
async def reject(
    lease_id: str,
    body: LeaseMutation,
    user: dict = Depends(require_human_user),
    capability: str | None = Header(None, alias=USER_DECISION_HEADER),
):
    return await _decide(
        reject_pending_lease,
        "reject",
        lease_id,
        body,
        user,
        capability,
    )


@router.post("/{lease_id}/revoke")
async def revoke(
    lease_id: str,
    body: LeaseMutation,
    user: dict = Depends(require_human_user),
    capability: str | None = Header(None, alias=USER_DECISION_HEADER),
):
    return await _decide(
        revoke_active_lease,
        "revoke",
        lease_id,
        body,
        user,
        capability,
    )
