"""Native safe-boundary adapter over the canonical message FIFO."""

from __future__ import annotations

import uuid
from dataclasses import replace

from core.turn_iteration import NativeSafeBoundaryInput
from infra.db_async import run_in_db
from infra.input_writes import (
    fence_batch_write,
    release_batch_before_write,
    settle_batch_write,
)
from infra.thread_inputs import ClaimedBatch, claim_safe_boundary, safe_input_prefixes
from infra.time import utc_now_iso


async def _broadcast(batch: ClaimedBatch) -> None:
    from infra.send_broadcast import broadcast_message_rows

    await broadcast_message_rows(
        batch.thread_id,
        tuple(item.message_id for item in batch.items),
    )


class _BoundaryWrite:
    def __init__(self, batch: ClaimedBatch, thread_row: object) -> None:
        self.batch = batch
        self.thread_row = thread_row
        self.attempt_id = str(uuid.uuid4())

    async def before_write(self) -> None:
        await fence_batch_write(
            self.batch,
            attempt_id=self.attempt_id,
            started_at=utc_now_iso(),
        )
        if any(
            bool(item.delivery_options.get("touch_session_on_insert"))
            for item in self.batch.items
        ):
            from infra.queries import touch_thread

            await run_in_db(touch_thread, self.batch.thread_id)

    async def accept(self) -> None:
        await run_in_db(
            settle_batch_write, self.attempt_id, outcome="accepted",
        )
        await _broadcast(self.batch)
        from infra.send_effects import run_batch_accepted_effects

        await run_batch_accepted_effects(self.batch, self.thread_row)

    async def release(self) -> None:
        await run_in_db(release_batch_before_write, self.batch)
        await _broadcast(self.batch)

    async def ambiguous(self) -> None:
        await run_in_db(
            settle_batch_write,
            self.attempt_id,
            outcome="ambiguous",
            error="Native provider write outcome is unknown",
        )
        await _broadcast(self.batch)

    def as_input(self):
        from core.context_builder import ContextBuilder
        from infra.config import WORKSPACE
        from infra.database import get_db
        from infra.input_render import project_claimed_input
        from llm.resolution import resolve_execution_target

        db = get_db()
        try:
            target = resolve_execution_target(self.batch.target_model, db=db)
            rendered = project_claimed_input(
                db, self.batch, thread_row=self.thread_row, target=target,
            )
        finally:
            db.close()
        content = ContextBuilder(WORKSPACE)._build_user_content(
            rendered.content, rendered.media_projection, model=target.transport_model,
        )
        return NativeSafeBoundaryInput(
            content=content,
            input_identity=rendered.identity,
            before_provider_write=self.before_write,
            settle_delivery=self.accept,
            release_before_write=self.release,
            mark_ambiguous=self.ambiguous,
        ), target

    def fitting_input(self, max_input_tokens: int):
        from core.token_estimation import measure_request

        for prefix in safe_input_prefixes(self.batch):
            candidate, target = _BoundaryWrite(prefix, self.thread_row).as_input()
            count = measure_request({"messages": [{"role": "user", "content": candidate.content}]},
                                    target=target).tokens
            if count <= max_input_tokens:
                return prefix, candidate
        return None


async def native_safe_boundary_input(
    *,
    thread_id: str,
    thread_row: object,
    owner_turn_id: str,
    max_input_tokens: int,
):
    """Claim the FIFO prefix opened by Send for the current Native Turn."""
    batch = await run_in_db(
        claim_safe_boundary, thread_id, owner_turn_id,
    )
    if batch is None:
        return None
    await _broadcast(batch)
    write = _BoundaryWrite(batch, thread_row)
    try:
        fitted = await run_in_db(write.fitting_input, max_input_tokens)
    except BaseException:
        await write.release()
        raise
    if fitted is None:
        await write.release()
        return None
    prefix, candidate = fitted
    if len(prefix.items) < len(batch.items):
        suffix = replace(batch, items=batch.items[len(prefix.items):])
        await run_in_db(release_batch_before_write, suffix)
        await _broadcast(suffix)
    return candidate
