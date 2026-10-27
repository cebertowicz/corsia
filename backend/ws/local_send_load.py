"""Load a person's local send within its owned native Turn."""

import asyncio

from infra import state


async def load_for_person(request, prepared) -> None:
    from llm.local_model_load import ensure_loaded, omlx_seat_and_model
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
    stopped = asyncio.create_task(prepared.cancel_event.wait())
    try:
        await asyncio.wait((load, stopped), return_when=asyncio.FIRST_COMPLETED)
        if prepared.cancel_event.is_set():
            load.cancel()
            await asyncio.gather(load, return_exceptions=True)
            raise asyncio.CancelledError()
        await load
    finally:
        stopped.cancel()
