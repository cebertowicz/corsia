"""One fit attempt: render a candidate against the destination and measure it.

The fit question in ``infra.send_preflight`` climbs a ladder of progressively
cheaper replay selections. Every rung does the same two things -- render a
candidate, then measure the result against the destination window -- and
differs only in *which* candidate it offers and what it does with a refusal.

This module owns the rung. ``infra.send_preflight`` owns the ladder: which
candidate to try, in what order, and when to give up.

The invariants of a single fit question (the thread, the destination, the input
being measured) are bound once into ``DestinationAttempt`` and reused by every
rung. They were previously threaded through two nested closures as twelve
repeated keyword arguments, which is what made the ladder hard to read and easy
to get wrong when only one of the two call sites was edited.

Rendering itself stays in ``infra.preflight_render``: it has three callers in
three modules and is not private to the fit question.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import infra.preflight_render as preflight_render
from llm.resolution import ExecutionTarget

if TYPE_CHECKING:
    from core.media_projection import ValidatedMediaProjection


async def prepare_idle_prefix(batch, *, thread_row, target):
    """Fit one idle FIFO prefix with the existing renderer and write ownership."""
    from infra.db_async import run_in_db
    from infra.input_render import prepare_claimed_batch
    from infra.input_writes import release_batch_before_write

    first_active = next(
        index for index, item in enumerate(batch.items)
        if not (item.role == "system" and item.metadata.get("type") == "context_event"
                and item.metadata.get("event_disposition") == "passive")
    )
    for length in range(len(batch.items), first_active, -1):
        prefix = replace(batch, items=batch.items[:length])
        prepared = await prepare_claimed_batch(prefix, thread_row=thread_row, target=target)
        context = prepared.prepared_context
        ledger = context.fit_ledger if context is not None else None
        if ledger is None:
            raise ValueError("idle admission omitted the destination fit ledger")
        if ledger.rendered_input_tokens <= ledger.effective_window_tokens:
            break
    if len(prefix.items) < len(batch.items):
        await run_in_db(release_batch_before_write, replace(batch, items=batch.items[len(prefix.items):]))
    prepared.batch = prefix
    return prepared


async def live_input_fits(batch, live, thread_row, target, projection) -> bool:
    """Check true-steer input without waiting on a checkpoint under the live lock."""
    from core.context_replay import exact_replay
    from infra.input_render import projected_input_tokens
    from infra.db_async import run_in_db
    from infra.send_preflight import _destination_attempt
    from llm.context_window import effective_usage_window
    from subagents.harness_checkpoint_context import (
        external_measurement, read_external_checkpoint_context,
    )

    _, observed_window = external_measurement(live, target.harness, target.context_window)
    counter = live.session.tool_context
    has_receipt = (counter.input_measured if target.harness == "claude-code"
                   else counter.receipt_identity is not None)
    measured = counter.tokens if has_receipt or counter.input_seeded else None
    window = effective_usage_window(target.context_window, observed_window)
    if window is None:
        raise ValueError("live input has no resolved context window")
    if measured is not None and measured > window:
        return False
    context = await run_in_db(
        read_external_checkpoint_context, batch.thread_id, target.catalog_id, target.harness,
    )
    if context is None:
        raise ValueError("live input target changed before admission")
    installed = context.installed_checkpoint
    if installed is not None and installed.id != context.registered_checkpoint_id:
        # Adoption belongs to the settled-tool/next-turn owner, outside this lock.
        return False
    if measured is not None:
        return measured + projected_input_tokens(projection, target) <= window
    if installed is not None:
        from core.checkpoint_resume import installed_replay

        selection = await run_in_db(installed_replay, installed)
    else:
        selection = exact_replay(context.history)
    attempt = _destination_attempt(
        thread_id=batch.thread_id, thread_row=thread_row,
        provider="cc" if target.harness == "claude-code" else "codex", target=target,
        current_user_content=projection.content,
        current_user_source_content=projection.source_content,
        agent_envelope=projection.agent_envelope, attachments=list(projection.attachments),
        media_projection=projection.media_projection, input_identity=projection.identity,
    )
    result = await attempt.cold(selection, trigger_reason=None)
    return result.prepared_context.fit_ledger.rendered_input_tokens <= window


@dataclass(frozen=True)
class DestinationAttempt:
    """The invariants of one fit question, bound once.

    Every field is fixed for the whole ladder: the thread, the destination it
    is being measured against, and the input that must fit alongside whatever
    replay is chosen. Only the candidate changes from rung to rung.
    """

    thread_id: str
    thread_row: Any
    provider: str
    target: ExecutionTarget
    current_user_content: str
    current_user_source_content: str
    agent_envelope: Any
    attachments: list | None
    media_projection: ValidatedMediaProjection | None
    input_identity: str

    async def cold(self, selection, *, trigger_reason: str | None):
        """Render one replay selection cold and measure whether it fits."""
        from core.destination_preflight import result_for_prepared_context

        prepared = await preflight_render.render_preflight_selection(
            thread_id=self.thread_id,
            thread_row=self.thread_row,
            provider=self.provider,
            target=self.target,
            current_user_content=self.current_user_content,
            current_user_source_content=self.current_user_source_content,
            agent_envelope=self.agent_envelope,
            attachments=self.attachments,
            media_projection=self.media_projection,
            selection=selection,
            live_handle=False,
            input_identity=self.input_identity,
        )
        return result_for_prepared_context(
            prepared,
            trigger_reason=trigger_reason,
        )

    async def live(
        self,
        live_candidates,
        *,
        retained_provider_tokens: int,
        canonical_input_tokens: int | None,
        trigger_reason: str | None,
    ):
        """Measure against what the live provider handle is already holding.

        Raises ``ExternalPreparedContextStale`` when the handle can no longer
        be trusted to hold what it claims; the ladder treats that as the handle
        going unhealthy rather than as a refusal to fit.
        """
        from core.destination_preflight import result_for_prepared_context

        prepared = await preflight_render.render_preflight_selection(
            thread_id=self.thread_id,
            thread_row=self.thread_row,
            provider=self.provider,
            target=self.target,
            current_user_content=self.current_user_content,
            current_user_source_content=self.current_user_source_content,
            agent_envelope=self.agent_envelope,
            attachments=self.attachments,
            media_projection=self.media_projection,
            selection=None,
            live_handle=True,
            live_history_epoch=live_candidates.history_epoch,
            live_sealed_boundary=live_candidates.sealed_boundary,
            input_identity=self.input_identity,
            retained_provider_tokens=retained_provider_tokens,
            canonical_input_tokens=canonical_input_tokens,
        )
        return result_for_prepared_context(
            prepared,
            trigger_reason=trigger_reason,
        )
