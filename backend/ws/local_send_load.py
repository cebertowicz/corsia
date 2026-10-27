"""Load a person's local send within its owned native Turn."""

import asyncio

from infra import state
from llm.resolution import ExecutionTarget


def _local_readiness(target: ExecutionTarget):
    from infra.database import get_db
    from llm.local_model_load import readiness

    db = get_db()
    try:
        return readiness(target, db=db)
    finally:
        db.close()


def _consume_load_result(task: asyncio.Task) -> None:
    if not task.cancelled():
        task.exception()


async def load_for_person(request, prepared) -> None:
    from infra.db_async import run_in_db
    from infra.send_target import LOCAL_LOAD_RUNNABLE, model_load_error
    from llm.local_model_load import ModelLoadFailed, ensure_loaded

    readiness = await run_in_db(_local_readiness, request.target)
    if prepared.cancel_event.is_set():
        raise asyncio.CancelledError()
    if readiness is None:
        return
    if readiness.state not in LOCAL_LOAD_RUNNABLE:
        raise RuntimeError(model_load_error(readiness.state))
    # A cached 'loaded' still goes through ensure_loaded's live read, so a model unloaded
    # since the last sweep is loaded with its fit check rather than by oMLX on the chat request.
    if readiness.state != "loaded":
        state.broadcast_to_thread(request.thread_id, {
            "type": "model_loading", "thread_id": request.thread_id,
            "turn_id": prepared.identity.turn_id, "model": request.target.catalog_id,
            "input_ids": list(request.input_ids),
        })
    load = asyncio.create_task(ensure_loaded(request.target))
    load.add_done_callback(_consume_load_result)
    stopped = asyncio.create_task(prepared.cancel_event.wait())
    try:
        await asyncio.wait((load, stopped), return_when=asyncio.FIRST_COMPLETED)
        if prepared.cancel_event.is_set():
            raise asyncio.CancelledError()
        try:
            await load
        except ModelLoadFailed as error:
            raise RuntimeError(model_load_error(error.cause)) from error
    finally:
        stopped.cancel()
