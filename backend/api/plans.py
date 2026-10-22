"""Authenticated plan projection, mutation, and decision API."""

from __future__ import annotations

from functools import partial
import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Response

from infra.auth import require_human_user, require_user
from infra.database import get_db
from infra.db_async import run_in_db, run_read
from infra import thread_updates
from infra.send_types import SendRejected

from plans import public

from plans.public import DecisionInputError, PlanActor, mutate

router = APIRouter(prefix="/api/plans", tags=["plans"])
logger = logging.getLogger(__name__)


def _human_actor(user_id: str) -> PlanActor:
    """Build one server-bound authenticated human plan actor."""

    return PlanActor(None, user_id, None, kind="user")


def _check_result(result):
    """Translate boundary failures into authenticated HTTP errors.

    409 means the target moved and the caller must re-read it. 422 means the
    request itself was refused and the caller can fix it in place. Collapsing
    the two makes every client message a guess.
    """

    if result == "forbidden":
        raise HTTPException(403, "Plan is outside the current actor scope")
    if isinstance(result, dict) and "input_error" in result:
        raise HTTPException(422, result["input_error"])
    if isinstance(result, dict) and "error" in result:
        raise HTTPException(result.get("status", 409), result["error"])
    return result


def _operator_operation(kind: str, target_id: str | None, user_id: str):
    """Execute one authenticated contextual plan projection."""
    db = get_db()
    actor = _human_actor(user_id)
    try:
        if kind == "workspace":
            return public.read_operator_workspace(actor=actor, db=db)
        if kind == "project" and target_id is not None:
            return public.read_operator_project(target_id, actor=actor, db=db)
        if kind == "thread" and target_id is not None:
            return public.read_operator_thread(target_id, actor=actor, db=db)
        if kind == "child" and target_id is not None:
            return public.read_operator_child(target_id, actor=actor, db=db)
        raise RuntimeError(f"unsupported operator projection kind: {kind}")
    except public.OperatorNotFound:
        return {"operator_error": "not_found"}
    finally:
        db.close()


def _etag_matches(if_none_match: str | None, etag: str) -> bool:
    """Match a strong revision ETag in a standard comma-separated header."""
    if not if_none_match:
        return False
    return any(candidate.strip() == etag for candidate in if_none_match.split(","))


def _activity_operation(kind: str, target_id: str | None, user_id: str):
    """Execute one authenticated compact Activity scope projection."""
    db = get_db()
    actor = _human_actor(user_id)
    try:
        if kind == "workspace":
            return public.read_activity_workspace(actor=actor, db=db)
        if kind == "project" and target_id is not None:
            return public.read_activity_project(target_id, actor=actor, db=db)
        raise RuntimeError(f"unsupported Activity projection kind: {kind}")
    except public.OperatorNotFound:
        return {"operator_error": "not_found"}
    finally:
        db.close()


async def _conditional_read(operation, user: dict, response: Response, if_none_match: str | None):
    """Return one revision-token payload or a content-free conditional response."""
    result = await run_read(partial(operation, user["id"]))
    error = result.get("operator_error") if isinstance(result, dict) else None
    if error == "not_found":
        raise HTTPException(404, "Plan target is unavailable")
    etag = f'"{result["revision_token"]}"'
    if _etag_matches(if_none_match, etag):
        return Response(status_code=304, headers={"ETag": etag})
    response.headers["ETag"] = etag
    return result


async def _operator_read(
    kind: str,
    target_id: str | None,
    user: dict,
    response: Response,
    if_none_match: str | None,
):
    """Return one operator payload or a content-free conditional response."""
    operation = partial(_operator_operation, kind, target_id)
    return await _conditional_read(operation, user, response, if_none_match)


@router.get("/activity/scopes/workspace")
async def read_activity_workspace(
    response: Response,
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
    user: dict = Depends(require_user),
):
    """Read the selected workspace's compact Activity index."""
    operation = partial(_activity_operation, "workspace", None)
    return await _conditional_read(operation, user, response, if_none_match)


@router.get("/activity/scopes/projects/{project_id}")
async def read_activity_project(
    project_id: str,
    response: Response,
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
    user: dict = Depends(require_user),
):
    """Read one accessible project's compact Activity index."""
    operation = partial(_activity_operation, "project", project_id)
    return await _conditional_read(operation, user, response, if_none_match)


@router.get("/operator/scopes/workspace")
async def read_operator_workspace(
    response: Response,
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
    user: dict = Depends(require_user),
):
    """Read the selected workspace's canonical operator activity rollup."""
    return await _operator_read("workspace", None, user, response, if_none_match)


@router.get("/operator/scopes/projects/{project_id}")
async def read_operator_project(
    project_id: str,
    response: Response,
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
    user: dict = Depends(require_user),
):
    """Read one accessible project's canonical operator activity rollup."""
    return await _operator_read("project", project_id, user, response, if_none_match)


@router.get("/operator/threads/{thread_id}")
async def read_operator_thread(
    thread_id: str,
    response: Response,
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
    user: dict = Depends(require_user),
):
    """Read one accessible thread through the shared operator contract."""
    return await _operator_read("thread", thread_id, user, response, if_none_match)


def _thread_view_operation(thread_id: str, body: dict[str, Any], user_id: str):
    """Record one pane view of a thread's plan on a worker database connection."""
    db = get_db()
    try:
        return public.record_thread_view(
            thread_id,
            seen_through=body.get("seen_through"),
            actor=_human_actor(user_id),
            db=db,
        )
    except public.OperatorNotFound:
        return {"operator_error": "not_found"}
    except public.PaneViewInputError as exc:
        return {"input_error": str(exc)}
    finally:
        db.close()


@router.post("/operator/threads/{thread_id}/seen")
async def record_operator_thread_seen(
    thread_id: str, body: dict[str, Any], user: dict = Depends(require_user),
):
    """Record the pane showed this plan, and nudge the chat row its failed dot clears from."""
    result = await run_in_db(partial(_thread_view_operation, thread_id, body, user["id"]))
    if result.get("operator_error") == "not_found":
        raise HTTPException(404, "Plan target is unavailable")
    checked = _check_result(result)
    thread_updates.notify_changed(user["id"], [thread_id])
    return checked


def _thread_states_operation(thread_ids: list[str], user_id: str):
    db = get_db()
    try:
        return public.read_thread_states(
            thread_ids, actor=_human_actor(user_id), db=db,
        )
    finally:
        db.close()


@router.get("/operator/thread-states")
async def read_operator_thread_states(ids: str = "", user: dict = Depends(require_user)):
    """Read the chat-list state of each named thread (comma-separated ids)."""
    thread_ids = list(dict.fromkeys(value for value in ids.split(",") if value))
    if len(thread_ids) > public.THREAD_STATE_LIMIT:
        raise HTTPException(422, f"read at most {public.THREAD_STATE_LIMIT} threads at once")
    return await run_read(partial(_thread_states_operation, thread_ids, user["id"]))


@router.get("/operator/nodes/{node_id}/child")
async def read_operator_child(
    node_id: str,
    response: Response,
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
    user: dict = Depends(require_user),
):
    """Read one attached child plan through a bounded operator payload."""
    return await _operator_read("child", node_id, user, response, if_none_match)


def _intake_operation(run):
    """Execute one intake operation on a worker connection with shared errors."""

    db = get_db()
    try:
        return run(db)
    except PermissionError:
        return "forbidden"
    except DecisionInputError as exc:
        return {"input_error": str(exc)}
    except public.PlanError as exc:
        return {"error": str(exc), "status": getattr(exc, "status", 409)}
    finally:
        db.close()


@router.post("/decisions/{decision_id}/answer")
async def answer_plan_decision(
    decision_id: str, body: dict[str, Any], user: dict = Depends(require_human_user),
):
    """Record one authenticated human's answer to a raised plan decision."""
    mutation_id = body.get("mutation_id")
    if not isinstance(mutation_id, str) or not mutation_id.strip():
        raise HTTPException(422, "mutation_id is required")
    if "actor_session_id" in body:
        raise HTTPException(422, "HTTP decision answers record the authenticated human")
    try:
        result = await run_in_db(partial(
            _intake_operation,
            lambda db: public.answer_decision(
                decision_id,
                option_id=body.get("option_id"), option_ids=body.get("option_ids"),
                other_text=body.get("other_text"),
                expected_revision=body.get("expected_revision"),
                mutation_id=mutation_id,
                actor=_human_actor(user["id"]),
                provenance="api.plans.decision",
                db=db,
            ),
        ))
    except SendRejected as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    response = _check_result(public.deliver_deferred(result))
    from jobs.corrections_recording import notify_decision_answer  # best effort; never fails the answer
    notify_decision_answer(decision_id, body.get("other_text"))
    public.drive_after_response(result)
    return response


def _settlement_workers_operation(plan_id, operation, params, mutation_id, user_id):
    """Authorize a fresh user settlement and discover workers to Stop first."""
    db = get_db()
    try:
        return public.prepare_user_settlement(
            plan_id,
            operation,
            params,
            actor=_human_actor(user_id),
            mutation_id=mutation_id,
            db=db,
        )
    except PermissionError:
        return "forbidden"
    except public.PlanInputError as exc:
        return {"input_error": str(exc)}
    except public.PlanError as exc:
        return {"error": str(exc)}
    finally:
        db.close()


def _mutation_operation(plan_id, operation, params, mutation_id, user_id):
    """Execute one authenticated mutation on a worker database connection."""

    db = get_db()
    try:
        return mutate(
            plan_id,
            operation,
            params,
            actor=_human_actor(user_id),
            provenance="api.plans",
            mutation_id=mutation_id,
            db=db,
        )
    except PermissionError:
        return "forbidden"
    except public.PlanInputError as exc:
        # Nothing moved. A malformed body stays malformed however many times it
        # is re-sent, and a lane-owned node is reachable through the operation
        # the message names, so both are fixable in place rather than by
        # re-reading. Everything else that reaches here did move. One class, not
        # a tuple: a sibling added here and forgotten on the tool surface is
        # exactly how this route and `tools/plans.py` drifted apart.
        return {"input_error": str(exc)}
    except public.PlanError as exc:
        return {"error": str(exc)}
    finally:
        db.close()


@router.post("/{plan_id}/mutations")
async def mutate_plan(plan_id: str, body: dict[str, Any], user: dict = Depends(require_user)):
    """Apply one actor-bound, audited plan mutation."""
    operation = body.get("operation")
    mutation_id = body.get("mutation_id")
    params = body.get("params")
    if not isinstance(operation, str) or not operation:
        raise HTTPException(422, "operation is required")
    if not isinstance(mutation_id, str) or not mutation_id.strip():
        raise HTTPException(422, "mutation_id is required")
    if "actor_session_id" in body:
        raise HTTPException(422, "HTTP mutations record the authenticated human, not a selected thread")
    if not isinstance(params, dict):
        raise HTTPException(422, "params must be an object")

    workers: tuple[str, ...] = ()
    if operation in {"mark_done", "close_node"}:
        workers = _check_result(await run_in_db(partial(
            _settlement_workers_operation,
            plan_id, operation, params, mutation_id, user["id"],
        )))
    failed_stops: list[str] = []
    for worker in workers:
        try:
            await _stop_worker_turn(user["id"], worker)
        except Exception:  # noqa: BLE001 - try every worker, but settle none on any failure
            logger.exception("Stopping worker %s before settlement failed", worker)
            failed_stops.append(worker)
    if failed_stops:
        raise HTTPException(
            409, "The worker could not be stopped, so the task was not settled. Try again.",
        )

    result = await run_in_db(partial(
        _mutation_operation,
        plan_id, operation, params, mutation_id, user["id"],
    ))
    response = _check_result(public.deliver_deferred(result))
    public.drive_after_response(result)
    return response


async def _stop_worker_turn(user_id: str, thread_id: str) -> None:
    """Stop one worker before the user's settlement may retire it.

    A worker with no running Turn has nothing to stop. Lookup, delivery and a
    stale-active receipt all fail the request before its plan write, so the
    route cannot report settlement while that worker continues running.
    """
    import infra.state as state
    from ws.stop_control import do_stop, resolve_active_turn_id
    from ws.thread_control import owned_thread

    user = {"id": user_id}
    turn_id = await resolve_active_turn_id(user, thread_id)
    if turn_id is None:
        return
    receipt = await do_stop(
        user, thread_id, state._active_turns.get(thread_id),
        state._active_llm_tasks.get(thread_id), turn_id,
        owns_thread_fn=lambda owner, sid: _owns(owned_thread, owner, sid),
    )
    if receipt.outcome == "stale_active":
        raise RuntimeError("the retired worker changed active Turns before Stop applied")


async def _owns(owned_thread, user: dict, thread_id: str) -> bool:
    return await owned_thread(user, thread_id) is not None
