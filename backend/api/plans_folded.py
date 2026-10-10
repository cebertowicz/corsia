"""Authenticated folded-history reads of a plan (split from ``api.plans``)."""

from __future__ import annotations

from functools import partial

from fastapi import APIRouter, Depends, HTTPException

from api.plans import _check_result, _human_actor
from infra.auth import require_user
from infra.database import get_db
from infra.db_async import run_read
from plans import public

router = APIRouter(prefix="/api/plans", tags=["plans"])


def _folded_operation(read, user_id: str):
    """Run one folded-history read on a worker connection; refusals are values, not raises."""
    db = get_db()
    try:
        return read(actor=_human_actor(user_id), db=db)
    except public.OperatorNotFound:
        return {"operator_error": "not_found"}
    except ValueError as exc:
        return {"input_error": str(exc)}
    finally:
        db.close()


async def _folded_read(read, user: dict):
    result = await run_read(partial(_folded_operation, read, user["id"]))
    if result.get("operator_error") == "not_found":
        raise HTTPException(404, "Plan target is unavailable")
    return _check_result(result)


@router.get("/{plan_id}/folded")
async def read_plan_folded(
    plan_id: str, parent: str = "root", cursor: str | None = None,
    user: dict = Depends(require_user),
):
    """Page the folded rows beneath one level of a plan (``parent``: a node id or ``root``)."""
    return await _folded_read(
        partial(public.read_folded, plan_id, parent=parent, cursor=cursor), user,
    )


@router.get("/{plan_id}/nodes/{node_id}/path")
async def read_plan_node_path(plan_id: str, node_id: str, user: dict = Depends(require_user)):
    """Place one node: its ancestors and the folded page it sits on."""
    return await _folded_read(partial(public.read_folded_path, plan_id, node_id), user)


@router.get("/{plan_id}/nodes/{node_id}")
async def read_plan_node(plan_id: str, node_id: str, user: dict = Depends(require_user)):
    """Read one node as the pane lists a row: the details a folded-history row leaves off."""
    return await _folded_read(partial(public.read_operator_node, plan_id, node_id), user)
