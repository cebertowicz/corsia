"""Load a person's local send within its owned native Turn."""

import asyncio
import logging

from infra import state
from llm.local_model_load import ModelLoadFailed

log = logging.getLogger(__name__)


def _consume_load_result(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    failure = task.exception()
    if failure is not None and not isinstance(failure, ModelLoadFailed):
        log.error("Local model load failed unexpectedly", exc_info=failure)


async def load_for_person(request, prepared) -> None:
    from infra.send_target import model_load_error, omlx_seat_and_model
    from llm.local_model_load import ensure_loaded
    from llm.network_discovery import get_loaded_model_ids

    if prepared.cancel_event.is_set():
        raise asyncio.CancelledError()
    seat = omlx_seat_and_model(request.target)
    if seat is not None and seat[1] not in get_loaded_model_ids(seat[0]):
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
