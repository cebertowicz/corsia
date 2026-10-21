"""Run one hidden, fresh Native turn through the canonical product path."""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from collections.abc import Awaitable, Callable
from functools import partial

from eval.turn_result import EvalTurnResult, read_turn_result


@dataclass(frozen=True)
class FreshTurn:
    """The canonical projection or an explicit error from one fresh turn."""

    thread_id: str
    result: EvalTurnResult | None
    error: str | None
    cleanup: Callable[[], Awaitable[None]] | None = None
    context_receipt: object | None = None


@dataclass(frozen=True)
class FreshTurnScope:
    """The existing project/thread scope whose normal delivery the fresh turn uses."""

    project_id: str | None = None
    owner_thread_id: str | None = None
    fenced_target: object | None = None


def _new_eval_session_id() -> str:
    return f"eval-{uuid.uuid4().hex[:12]}"


async def _open_eval_thread(
    user_id: str, model: str, *, scope: FreshTurnScope | None = None,
) -> tuple[str, dict]:
    """Create the fresh turn in its target's existing scope and return its dispatch row."""
    from infra.db_async import run_in_db
    from infra.fresh_threads import create_fresh_thread

    thread_id = _new_eval_session_id()
    await run_in_db(partial(
        create_fresh_thread, user_id=user_id, thread_id=thread_id,
        title="Eval turn", model=model,
        project_id=None if scope is None else scope.project_id,
        parent_thread_id=None,
        spawned_by=None,
    ))
    return thread_id, await run_in_db(_read_thread_row, thread_id)


def _read_thread_row(thread_id: str) -> dict:
    """Read the freshly created row as the canonical dispatch input."""
    from infra.database import get_db

    db = get_db()
    try:
        row = db.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
    finally:
        db.close()
    if row is None:
        raise RuntimeError(f"Eval thread {thread_id} disappeared after creation")
    return dict(row)


def _failure_detail(error: BaseException) -> str:
    """Prefer a rejection's caller-facing detail over its bare representation."""
    detail = getattr(error, "detail", None)
    return detail if isinstance(detail, str) and detail else str(error)


async def _run_turn(
    *, thread_id: str, thread_row: dict, prompt: str, model: str, timeout_seconds: float,
) -> tuple[str | None, str | None]:
    """Admit and settle exactly one Native turn without a passive digest."""
    from core.turn_core import BOOT_EPOCH
    from infra.db_async import run_in_db
    from infra.send_types import InboxQueuedInput
    from infra.thread_input_types import FailedInputBatch
    from infra.thread_driver import admit_input
    from infra.thread_execution import execute_claimed_batch
    from infra.turn_admission_driver import drive_turn_admission
    from infra.time import utc_now_iso
    from llm.resolution import resolve_execution_target

    try:
        target = await run_in_db(resolve_execution_target, model)
        admission = await run_in_db(
            admit_input,
            thread_id,
            role="user",
            content=prompt,
            intent="apply_now",
            message_id=str(uuid.uuid4()),
            turn_id=str(uuid.uuid4()),
            boot_epoch=BOOT_EPOCH,
            admitted_at=utc_now_iso(),
            metadata={"sender_kind": "eval"},
            delivery_options={"native_mode": "delegation", "attachments": []},
            materialize_passive_digest=False,
        )
    except Exception as exc:
        return None, f"Eval turn errored: {_failure_detail(exc)}"
    if admission.held_for_restart:
        return None, "Eval turn is held during restart"
    batch = admission.batch
    if isinstance(batch, InboxQueuedInput):
        return None, "Eval turn was not handed to a Native runtime"
    if batch is None:
        return None, "Eval turn could not claim an idle Turn for its input"
    if isinstance(batch, FailedInputBatch):
        await drive_turn_admission(batch)
        return None, f"Eval turn errored: {batch.error_text}"
    try:
        send_result = await execute_claimed_batch(
            batch,
            thread_row=thread_row,
            target=target,
            user={"id": str(thread_row.get("user_id") or "")},
        )
    except Exception as exc:
        return None, f"Eval turn errored: {_failure_detail(exc)}"
    if isinstance(send_result, InboxQueuedInput) or send_result.turn_task is None:
        return None, "Eval turn was not handed to a Native runtime"
    try:
        await asyncio.wait_for(send_result.turn_task, timeout=timeout_seconds)
    except asyncio.TimeoutError:
        return None, f"Eval turn timed out after {timeout_seconds}s"
    except Exception as exc:
        return None, f"Eval turn errored: {_failure_detail(exc)}"
    if not send_result.turn_id:
        return None, "Eval turn completed without a Turn identity"
    return send_result.turn_id, None


async def run_fresh_turn(
    *, user_id: str, prompt: str, model: str, timeout_seconds: float,
    scope: FreshTurnScope | None = None,
) -> FreshTurn:
    """Return one fresh turn's canonical tool-call projection or its error."""
    thread_id, thread_row = await _open_eval_thread(user_id, model, scope=scope)
    cleanup = None
    context_receipt = None
    if scope is not None and scope.fenced_target is not None:
        from eval.fresh_turn_context import attach_fenced_context, detach_and_delete
        from infra.db_async import run_in_db

        try:
            receipt = await run_in_db(
                attach_fenced_context,
                thread_id,
                user_id,
                owner_thread_id=scope.owner_thread_id or "",
                project_id=scope.project_id,
                target=scope.fenced_target,
            )
            cleanup = lambda: detach_and_delete(receipt)
            context_receipt = receipt
        except Exception as exc:
            from api.thread_deletion import delete_owned_thread

            await delete_owned_thread(thread_id, user_id, [])
            return FreshTurn(thread_id, None, f"Eval context setup errored: {_failure_detail(exc)}")
    from eval.tool_allowlist import carry_in_tool_isolation

    with carry_in_tool_isolation():
        turn_id, error = await _run_turn(
            thread_id=thread_id,
            thread_row=thread_row,
            prompt=prompt,
            model=model,
            timeout_seconds=timeout_seconds,
        )
    if turn_id is None:
        return FreshTurn(thread_id, None, error, cleanup, context_receipt)
    return FreshTurn(thread_id, await read_turn_result(thread_id, turn_id), None, cleanup, context_receipt)
