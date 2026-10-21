"""Workspace and project provider-policy API."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, StrictBool, StrictInt

from api.locality_policy_guard import require_person_for_locality
from connections.provider_policy import policy_payload, set_policy, set_use_workspace
from llm.locality import project_cloud_models_off, set_project_cloud_models_off
from connections.vendor_accounts import AccountError
from infra.auth import require_user
from infra.database import get_db
from infra.db_async import run_in_db
from infra.product_events import emit_product_event
from infra.state.ws import notify_models_changed

router = APIRouter(prefix="/api", tags=["provider-policy"], dependencies=[Depends(require_user)])


class ProviderPolicyWrite(BaseModel):
    expected_revision: StrictInt = Field(ge=0)
    rollover: StrictBool
    order: list[str] | None = None
    disabled: list[str] = []


class UseWorkspaceWrite(BaseModel):
    expected_revision: StrictInt = Field(ge=0)
    use_workspace: StrictBool


class CloudModelsOffWrite(BaseModel):
    cloud_models_off: StrictBool


def _call(fn, *args, **kwargs):
    db = get_db()
    try:
        return fn(*args, db=db, **kwargs)
    finally:
        db.close()


async def _run(fn, *args, **kwargs):
    try:
        return await run_in_db(_call, fn, *args, **kwargs)
    except AccountError as error:
        raise HTTPException(409, str(error)) from error


@router.get("/provider-policy")
async def get_provider_policy(project_id: str | None = None):
    return await _run(policy_payload, project_id)


@router.put("/provider-policy/{provider_key}")
async def put_provider_policy(provider_key: str, body: ProviderPolicyWrite, project_id: str | None = None):
    return await _run(set_policy, project_id, provider_key, rollover=body.rollover, order=body.order,
                      disabled=body.disabled, expected_revision=body.expected_revision)


@router.put("/projects/{project_id}/provider-policy/use-workspace")
async def put_use_workspace(project_id: str, body: UseWorkspaceWrite):
    return await _run(set_use_workspace, project_id, use_workspace=body.use_workspace,
                      expected_revision=body.expected_revision)


@router.get("/projects/{project_id}/cloud-models-off")
async def get_cloud_models_off(project_id: str):
    enabled = await _run(project_cloud_models_off, project_id)
    return {"project_id": project_id, "cloud_models_off": enabled}


@router.put("/projects/{project_id}/cloud-models-off")
async def put_cloud_models_off(
    project_id: str, body: CloudModelsOffWrite, user: dict = Depends(require_user),
):
    require_person_for_locality(user, subject="project_cloud_models_off", name=project_id)
    result = await _run(
        set_project_cloud_models_off, project_id, cloud_models_off=body.cloud_models_off,
    )
    # Held heads and every catalog reader depend on this switch: resume what it released.
    notify_models_changed()
    emit_product_event(
        "project", "cloud_models_off.changed",
        properties={"project_id": project_id, "cloud_models_off": body.cloud_models_off},
    )
    return result
