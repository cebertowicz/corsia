"""Launch document review passes through the shared disposable-fork handles."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

from core.artifact_review_fork_kind import ARTIFACT_REVIEW_KIND
from core.fork_runtime import ForkHandle, cancel_fork, forget, handles
from core.artifact_review_capture import submitted


def _instruction(row: Any, artifact: Any, note: str) -> str:
    document = artifact["path"] or artifact["external_id"] or row["artifact_id"]
    return (
        f"Review the document {document!s}. Request: {note}\n\n"
        "Use only the document comment tools. Create review comments or suggestions as needed, "
        "then call doc_feedback_send for this document. Do not send messages, spawn work, or write files."
    )


def _cold_request(request_id: str):
    """Freeze the latest installed checkpoint plus the reviewer's sealed tail."""
    from core.checkpoint_render import build_cold_checkpoint_admission, checkpoint_binary
    from history.projection import render_portable_history
    from history.types import HistoryBoundary
    from infra.checkpoint_queries import current_installed
    from infra.database import get_db
    from llm.resolution import resolve_thread_target
    from subagents.codex_runtime_support import resolve_working_dir

    db = get_db()
    try:
        row = db.execute("SELECT * FROM doc_review_requests WHERE id=?", (request_id,)).fetchone()
        if row is None or row["cancelled_at"] or row["answered_batch_id"] or not row["fork_id"]:
            return None
        thread = db.execute("SELECT * FROM threads WHERE id=?", (row["reviewer_thread_id"],)).fetchone()
        artifact = db.execute("SELECT * FROM artifact_identities WHERE id=?", (row["artifact_id"],)).fetchone()
        if thread is None or artifact is None:
            return None
        target = resolve_thread_target(str(thread["model"] or ""), thread_id=thread["id"], db=db)
        if not target.available:
            return None
        previous = current_installed(thread["id"], db=db)
        projection = render_portable_history(
            thread["id"], db=db,
            start_exclusive=HistoryBoundary.of_checkpoint(previous) if previous else None,
        )
        boundary = projection.sealed_boundary
        if boundary is None:
            return None
        admission = build_cold_checkpoint_admission(
            checkpoint_id=row["fork_id"], thread_id=thread["id"], trigger="agent",
            source_target=target, writer_target=target, boundary=boundary, previous=previous,
            projection=projection, working_dir=resolve_working_dir(thread["project_id"], db),
            reasoning_effort=thread["reasoning_effort"], binary=checkpoint_binary(target, db), db=db,
        )
        from doceditor.review_fork_intent import review_intent
        from subagents.harness_tool_bridge import build_tool_registry

        return row, replace(
            admission.provider_request, instruction=_instruction(row, artifact, review_intent(db, row)),
            tool_registry=build_tool_registry(admission.provider_request.working_dir),
        )
    finally:
        db.close()


async def start(request_id: str) -> None:
    """Freeze after any active checkpoint, then register exactly one review pass."""
    prepared = await asyncio.to_thread(_cold_request, request_id)
    if prepared is None:
        await ARTIFACT_REVIEW_KIND.limit_or_failure_handler(request_id)
        return
    row, request = prepared
    fork_id = request.checkpoint_id
    if fork_id in handles or await asyncio.to_thread(submitted, request_id):
        return
    cancel_event = asyncio.Event()
    task = asyncio.create_task(_run(request_id, request, cancel_event))
    handles[fork_id] = ForkHandle(
        task, cancel_event, request.thread_id, kind=ARTIFACT_REVIEW_KIND, request=request,
    )
    task.add_done_callback(lambda done: forget(fork_id, done))


def cancel(fork_id: str) -> None:
    cancel_fork(fork_id)


async def _source(request):
    from core.checkpoint_render import capture_warm_checkpoint_source
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
    source = await capture_warm_checkpoint_source(
        request.thread_id, request.cold_source.projection.sealed_boundary, target=request.target,
        binary=request.binary, reasoning_effort=request.reasoning_effort,
    )
    if source is None:
        return request
    if request.target.harness == "claude-code" and not source.invocation.mcp_config:
        return request
    warm = replace(request, warm_source=source)
    if request.target.harness == "native":
        from core.checkpoint_native_fork import warm_input_fits

        if not await warm_input_fits(warm, kind=ARTIFACT_REVIEW_KIND):
            return request
    return warm


async def _run(request_id: str, request: Any, cancel_event: asyncio.Event) -> None:
    try:
        from core.checkpoint_runtime import await_checkpoint
        from infra.checkpoint_queries import active_for_thread

        active = await asyncio.to_thread(active_for_thread, request.thread_id)
        if active is not None:
            from core.checkpoint_native_tools import await_or_cancel

            await await_or_cancel(await_checkpoint(active.id), cancel_event)
            prepared = await asyncio.to_thread(_cold_request, request_id)
            if prepared is None:
                await ARTIFACT_REVIEW_KIND.limit_or_failure_handler(request_id)
                return
            _row, request = prepared
        from core.checkpoint_usage import bind_usage

        request = await bind_usage(await _source(request), checkpoint_owned=False)
        if request.target.harness == "native":
            from core.checkpoint_native_fork import invoke_checkpoint
        elif request.target.harness == "claude-code":
            from core.checkpoint_cc_fork import invoke_checkpoint
        elif request.target.harness == "codex":
            from core.checkpoint_codex_fork import invoke_checkpoint
        else:
            raise ValueError(f"unsupported artifact-review harness: {request.target.harness}")
        result = await invoke_checkpoint(request, cancel_event, kind=ARTIFACT_REVIEW_KIND)
        if not cancel_event.is_set():
            await ARTIFACT_REVIEW_KIND.end_handler(request_id, result)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        if not cancel_event.is_set():
            await ARTIFACT_REVIEW_KIND.limit_or_failure_handler(request_id, error)
