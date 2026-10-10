"""Record the lane candidate and its received-message closure together."""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from doceditor.live_document import LiveDocument, file_hash, yield_loop_pass
from doceditor.live_publication_record import Publication
from doceditor.live_store import PendingWrite
from doceditor.live_text import clone

if TYPE_CHECKING:
    from doceditor.live_lane import Operation


async def record_candidate(live: LiveDocument, fork, prior_hash: str,
                           publication: Publication | None, operation: Operation) -> PendingWrite:
    # Merging the candidate and cloning the durable state are each a full
    # pass over a large document; due timers run between them.
    await yield_loop_pass()
    async with live.committer_lock:
        messages = live.received_snapshot()
        durable = clone(live.durable.get_update())
        for message in messages:
            durable.apply_update(message.update)
        record = PendingWrite(
            operation.op_id, fork.get_update(durable.get_state()), fork.get_update(),
            prior_hash, file_hash(str(fork["text"]).encode("utf-8")), publication=publication,
        )
        ack = await asyncio.to_thread(live.store.record_pending, record, messages)
        live.pending_changed()
        live.committed(messages, ack)
    return record
