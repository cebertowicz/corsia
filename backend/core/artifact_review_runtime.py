"""Launch document review passes through the shared disposable-fork handles."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

from core.artifact_review_fork_kind import ARTIFACT_REVIEW_KIND
from core.fork_runtime import ForkHandle, cancel_fork, forget, handles
from core.artifact_review_capture import ReviewForkFailure, submitted

_starting: set[str] = set()


def _instruction(row: Any, artifact: Any, note: str) -> str:
    document = artifact["path"] or artifact["external_id"] or row["artifact_id"]
    return (
        f"Review the document {document!s}. Request: {note}\n\n"
        "Use only the document comment tools. Create review comments or suggestions as needed, "
        "then call doc_feedback_send for this document. Do not send messages, spawn work, or write files."
    )


def _cold_request(request_id: str):
    """Freeze the latest installed checkpoint plus the reviewer's sealed tail."""
    from core.checkpoint_render import render_prior_checkpoint
    from core.fork_render import fork_binary
    from core.fork_admission import build_cold_fork_admission
    from core.fork_types import ColdForkSource
    from core.artifact_review_source import bounded_review_history
    from infra.checkpoint_queries import current_installed
    from infra.database import get_db
    from llm.resolution import ModelUnavailableError, resolve_thread_target
    from subagents.codex_runtime_support import resolve_working_dir

    db = get_db()
    try:
        row = db.execute("SELECT * FROM doc_review_requests WHERE id=?", (request_id,)).fetchone()
        if row is None or row["state"] != "forked":
            raise ReviewForkFailure("row_state")
        thread = db.execute("SELECT * FROM threads WHERE id=?", (row["reviewer_thread_id"],)).fetchone()
        artifact = db.execute("SELECT * FROM artifact_identities WHERE id=?", (row["artifact_id"],)).fetchone()
        if thread is None or artifact is None:
            raise ReviewForkFailure("thread_or_artifact")
        try:
            target = resolve_thread_target(str(thread["model"] or ""), thread_id=thread["id"], db=db)
        except ModelUnavailableError as error:
            raise ReviewForkFailure("target_unavailable") from error
        if not target.available:
            raise ReviewForkFailure("target_unavailable")
        previous = current_installed(thread["id"], db=db)
        projection = bounded_review_history(thread["id"], previous, db=db)
        boundary = projection.sealed_boundary
        if boundary is None:
            raise ReviewForkFailure("no_sealed_boundary")
        from doceditor.review_fork_intent import review_intent
        from subagents.harness_tool_bridge import build_tool_registry

        protected = ({"role": "user", "content": render_prior_checkpoint(previous)},) if previous else ()
        request = build_cold_fork_admission(
            fork_id=row["fork_id"], thread_id=thread["id"], target=target, kind=ARTIFACT_REVIEW_KIND,
            instruction=_instruction(row, artifact, review_intent(db, row)),
            source=ColdForkSource(projection, protected),
            working_dir=resolve_working_dir(thread["project_id"], db),
            reasoning_effort=thread["reasoning_effort"], binary=fork_binary(target, db),
        )
        return row, replace(request, tool_registry=build_tool_registry(request.working_dir))
    finally:
        db.close()


async def start(request_id: str) -> None:
    """Freeze after any active checkpoint, then register exactly one review pass."""
    # Reserve before yielding: concurrent repeats never render history again.
    if request_id in _starting:
        return
    _starting.add(request_id)
    try:
        if await asyncio.to_thread(_already_started, request_id):
            return
        try:
            _row, request = await asyncio.to_thread(_cold_request, request_id)
        except Exception as error:
            from core.artifact_review_capture import failure_reason

            reason = failure_reason(error)
            if reason.startswith("provider_error:"):
                reason = f"cold_request_build:{type(error).__name__}"
            await ARTIFACT_REVIEW_KIND.limit_or_failure_handler(request_id, reason=reason)
            return
        if await asyncio.to_thread(submitted, request_id):
            return
        fork_id = request.fork_id
        cancel_event = asyncio.Event()
        task = asyncio.create_task(_run(request_id, request, cancel_event))
        handles[fork_id] = ForkHandle(
            task, cancel_event, request.thread_id, kind=ARTIFACT_REVIEW_KIND, request=request,
        )
        task.add_done_callback(lambda done: forget(fork_id, done))
    finally:
        _starting.discard(request_id)


def _already_started(request_id: str) -> bool:
    from infra.database import get_db

    db = get_db()
    try:
        row = db.execute("SELECT state,fork_id FROM doc_review_requests WHERE id=?", (request_id,)).fetchone()
        return row is None or row["state"] != "forked" or row["fork_id"] in handles
    finally:
        db.close()


def cancel(fork_id: str) -> None:
    cancel_fork(fork_id)


async def _source(request):
    from core.fork_render import capture_warm_fork_source
    from infra.database import get_db

    def busy():
        db = get_db()
        try:
            return db.execute(
                "SELECT 1 FROM active_turns WHERE thread_id=? AND status='running' LIMIT 1",
                (request.thread_id,),
            ).fetchone() is not None
        finally:
            db.close()

    if await asyncio.to_thread(busy):
        return request
    source = await capture_warm_fork_source(
        request.thread_id, request.cold_source.projection.sealed_boundary, target=request.target,
        binary=request.binary, reasoning_effort=request.reasoning_effort,
    )
    if source is None:
        return request
    if request.target.harness == "claude-code" and not source.invocation.mcp_config:
        return request
    warm = replace(request, warm_source=source)
    if request.target.harness == "native":
        from core.fork_native import warm_input_fits

        if not await warm_input_fits(warm, kind=ARTIFACT_REVIEW_KIND):
            return request
    return warm


async def _run(request_id: str, request: Any, cancel_event: asyncio.Event) -> None:
    try:
        from core.checkpoint_runtime import await_checkpoint
        from infra.checkpoint_queries import active_for_thread

        active = await asyncio.to_thread(active_for_thread, request.thread_id)
        if active is not None:
            from core.fork_native_tools import await_or_cancel

            await await_or_cancel(await_checkpoint(active.id), cancel_event)
            prepared = await asyncio.to_thread(_cold_request, request_id)
            _row, request = prepared
        request = await ARTIFACT_REVIEW_KIND.usage_binder(await _source(request))
        from core.fork_runtime import invoke_fork

        result = await invoke_fork(request, cancel_event, kind=ARTIFACT_REVIEW_KIND)
        if not cancel_event.is_set():
            await ARTIFACT_REVIEW_KIND.end_handler(request_id, result)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        if not cancel_event.is_set():
            await ARTIFACT_REVIEW_KIND.limit_or_failure_handler(request_id, error)
