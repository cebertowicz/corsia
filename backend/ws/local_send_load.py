"""Load a person's local send within its owned native Turn."""

import asyncio
from dataclasses import replace

from infra import state


async def ready_for_turn(target, *, load_for):
    """Resolve live local readiness before binding a native Turn."""
    from infra.db_async import run_in_db
    from infra.send_target import Admission
    from infra.unavailable_model_recovery import model_admission
    from llm.local_model_load import omlx_seat_and_model
    from llm.providers.omlx_lifecycle import read_loaded

    seat = omlx_seat_and_model(target)
    if seat is None:
        return target, Admission("run", load_for=load_for)
    loaded = await read_loaded({"server_id": seat[0]})
    if loaded is None:
        target = replace(target, available=False, availability_reason="unreachable")
    target, admission = await run_in_db(
        model_admission, target, load_for=load_for,
        loaded={row["identifier"] for row in loaded} if loaded is not None else None,
    )
    if admission.kind not in ("run", "load"):
        raise admission.error(target.catalog_id)
    return target, admission


async def load_for_person(request, prepared) -> None:
    from llm.local_model_load import ensure_loaded

    if prepared.cancel_event.is_set():
        raise asyncio.CancelledError()
    state.broadcast_to_thread(request.thread_id, {
        "type": "model_loading", "thread_id": request.thread_id,
        "turn_id": prepared.identity.turn_id, "model": request.target.catalog_id,
        "input_ids": list(request.load_for),
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
