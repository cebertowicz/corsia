"""Settle one durable pending barrier; never reconstruct item identities."""
from __future__ import annotations

import asyncio
from dataclasses import replace

from doceditor.live_document import file_hash
from doceditor.live_store import HeldChoice, StoreRefused
from doceditor.live_text import apply_text_async, clone


async def recover(lane) -> None:
    live = lane.live
    from tools.filesystem_effects import unlink_bound_file

    stage = live.path.with_name(f".corsia-live-{live.path.name}.stage")
    await asyncio.to_thread(unlink_bound_file, stage, missing_ok=True)
    pending = await asyncio.to_thread(live.store.pending)
    if pending is None:
        return
    current = file_hash(await asyncio.to_thread(live.path.read_bytes))
    async with live.committer_lock:
        if pending.state == "held":
            choice = pending.choice
            if choice is None:
                return
            if current != choice.result_hash:
                await asyncio.to_thread(live.store.clear_choice, pending.op_id)
                live.pending_changed()
                return
            await asyncio.to_thread(live.store.apply_choice, pending.op_id, choice)
            live.pending_changed()
            await live.apply_committed(choice.candidate, choice.mirror_state, choice.result_hash)
        elif current == pending.result_hash:
            await asyncio.to_thread(live.store.promote, pending.op_id)
            live.pending_changed()
            await live.apply_committed(pending.candidate, pending.mirror_state, pending.result_hash)
        elif current == pending.prior_hash:
            await asyncio.to_thread(live.store.discard, pending.op_id)
            live.pending_changed()
        else:
            await asyncio.to_thread(live.store.mark_held, pending.op_id, current)
            live.pending_changed()
    if lane.published:
        lane.published()


async def _choice_record(lane, pending, fork, current_hash, kind, publication):
    live = lane.live
    async with live.committer_lock:
        messages = live.received_snapshot()
        durable = clone(live.durable.get_update())
        for message in messages:
            durable.apply_update(message.update)
        choice = HeldChoice(
            kind, fork.get_update(durable.get_state()), fork.get_update(),
            current_hash, file_hash(str(fork["text"]).encode("utf-8")), publication=publication,
        )
        if kind == "file":
            ack = await asyncio.to_thread(live.store.apply_choice, pending.op_id, choice, messages)
        else:
            ack = await asyncio.to_thread(live.store.record_choice, pending.op_id, choice, messages)
        live.pending_changed()
        live.committed(messages, ack)
        if kind == "file":
            await live.apply_committed(choice.candidate, choice.mirror_state, choice.result_hash)
    return choice


async def choose(lane, kind, actor=None):
    """Runs as the lane's queued item, so a choice is reported even if its caller stopped."""
    from doceditor import live_events
    result = await _choose(lane, kind, actor)
    live_events.emit("choice_resolved", artifact_id=lane.artifact_id, store_id=lane.live.store.store_id,
                     choice=kind, server_saved=result.server_saved)
    return result


async def _choose(lane, kind, actor):
    from doceditor import live_lane
    from doceditor.live_lane import LaneRefused, Operation, WriteResult

    live = lane.live
    pending = await asyncio.to_thread(live.store.pending)
    if pending is None or pending.state != "held":
        raise LaneRefused("No outside-change choice is waiting")
    if pending.choice is not None:
        raise LaneRefused("The last choice has not settled; try again shortly")
    if kind not in ("file", "corsia"):
        raise LaneRefused("Unknown outside-change choice")
    data = await asyncio.to_thread(live.path.read_bytes)
    current_hash = file_hash(data)
    fork = clone(live.doc.get_update())
    if kind == "file":
        await apply_text_async(fork, data.decode("utf-8"))
    text = str(fork["text"])
    from doceditor.live_comments import candidate_ranges
    ranges = candidate_ranges(live, fork)
    await lane.validate_actor(text, actor)
    publication = await lane.policy(Operation("replace", content=text, actor=actor), text) if lane.policy else None
    if publication is not None:
        publication = replace(publication, ranges=ranges)
    choice = await _choice_record(lane, pending, fork, current_hash, kind, publication)
    if kind == "file":
        if lane.published:
            lane.published()
        return WriteResult(text, True)
    await live_lane._checkpoint("choice_record")
    try:
        await lane.write(text, current_hash)
    except Exception:
        # A refused clear leaves the complete held evidence and choice intact.
        # A later lane retry or load clears it; no new write can overtake it.
        await asyncio.to_thread(live.store.clear_choice, pending.op_id)
        live.pending_changed()
        raise
    await live_lane._checkpoint("choice_rename")
    server_saved = True
    try:
        await live_lane._checkpoint("choice_apply")
        async with live.committer_lock:
            await asyncio.to_thread(live.store.apply_choice, pending.op_id, choice)
            live.pending_changed()
            await live.apply_committed(choice.candidate, choice.mirror_state, choice.result_hash)
    except StoreRefused:
        server_saved = False
        live.apply_unsaved(choice.candidate)
    if server_saved and lane.published:
        lane.published()
    return WriteResult(text, server_saved)
