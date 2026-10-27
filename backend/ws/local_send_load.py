"""Load a person's local send within its owned native Turn."""

import asyncio

from infra import state


async def ready_for_turn(target, *, may_load):
    """Resolve live local readiness before binding a native Turn."""
    from infra.db_async import run_in_db
    from infra.send_target import classify_target_admission, ready_target
    from llm.local_model_load import omlx_seat_and_model
    from llm.providers.omlx_lifecycle import read_loaded
    from llm.resolution import ModelUnavailableError

    seat = omlx_seat_and_model(target)
    if seat is None:
        return target, False
    loaded = await read_loaded({"server_id": seat[0]})
    if loaded is None:
        raise ModelUnavailableError(target.catalog_id, "unreachable")
    target = await run_in_db(ready_target, target, loaded={row["identifier"] for row in loaded})
    admission = classify_target_admission(target, may_load=may_load)
    if admission.kind not in ("run", "load"):
        raise ModelUnavailableError(target.catalog_id, admission.cause or "model_unavailable")
    return target, admission.kind == "load"


async def load_for_person(request, prepared) -> None:
    from llm.local_model_load import ensure_loaded

    if prepared.cancel_event.is_set():
        raise asyncio.CancelledError()
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
            raise asyncio.CancelledError()
        await load
    finally:
        # Only this waiter stops: the seat's shared load is shielded and logs its own outcome.
        load.cancel()
        if load.done() and not load.cancelled():
            load.exception()
        stopped.cancel()
