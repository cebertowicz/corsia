"""Canonical provider rendering for one claimed input batch."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any

from core.message_history import AgentEnvelopeRender, render_agent_envelope
from infra.db_async import run_in_db
from infra.send_types import SendRejected
from infra.thread_inputs import ClaimedBatch, safe_input_prefixes

_PREAMBLE = (
    "CORSIA is delivering {count} queued inbound inputs together. "
    "Apply them in the numbered FIFO order shown below."
)


@dataclass(frozen=True, slots=True)
class RenderedInputBatch:
    """Exact text and content identity handed to a provider."""

    identity: str
    content: str


@dataclass(frozen=True, slots=True)
class InputProviderProjection:
    """Exact pending FIFO payload used to prove a model destination fits."""

    identity: str
    content: str
    source_content: str
    agent_envelope: Any
    attachments: tuple[dict[str, Any], ...]
    media_projection: Any | None
    receipts: tuple[Any, ...]


@dataclass(slots=True)
class PreparedBatch:
    """One rendered batch and its destination facts."""

    thread_row: dict
    target: Any
    provider: str
    is_subagent: bool
    content: str
    agent_envelope: Any
    attachments: list
    media_projection: Any
    prepared_context: Any
    refresh_prepared_context: Any
    runtime_truth: dict
    batch: ClaimedBatch | None = None


def projected_input_tokens(projection: InputProviderProjection, target) -> int:
    """Count the exact input used by both live admission and write accounting."""
    from core.token_estimation import measure_request, estimate_media_projection_tokens

    return measure_request(
        {"messages": [{"role": "user", "content": projection.content}]}, target=target,
    ).tokens + estimate_media_projection_tokens(projection.media_projection)


def _identity(batch: ClaimedBatch, content: str) -> str:
    payload = {
        "thread": batch.thread_id,
        "epoch": batch.target_execution_epoch,
        "model": batch.target_model,
        "members": [
            {
                "id": item.message_id,
                "order": item.input_order,
                "revision": item.revision,
                "role": item.role,
                "content": item.content,
                "intent": item.intent,
                "metadata": item.metadata,
                "delivery_options": item.delivery_options,
            }
            for item in batch.items
        ],
        "rendered": content,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "input-batch:" + hashlib.sha256(canonical.encode()).hexdigest()


def render_claimed_batch(
    batch: ClaimedBatch,
    *,
    frame_agent_inputs: bool = True,
) -> RenderedInputBatch:
    """Render member envelopes once, preserving the immutable FIFO order."""
    if not batch.items:
        raise ValueError("cannot render an empty input batch")
    rendered = [
        (
            render_agent_envelope(item.content, item.metadata).content
            if frame_agent_inputs
            else item.content
        )
        for item in batch.items
    ]
    if len(rendered) == 1:
        content = rendered[0]
    else:
        members = "\n\n".join(
            f"[{index}/{len(rendered)}]\n{text}"
            for index, text in enumerate(rendered, start=1)
        )
        content = f"{_PREAMBLE.format(count=len(rendered))}\n\n{members}"
    return RenderedInputBatch(_identity(batch, content), content)


def _batch_agent_envelope(
    batch: ClaimedBatch,
    source: RenderedInputBatch,
    content: str,
) -> AgentEnvelopeRender:
    members = tuple(
        render_agent_envelope(item.content, item.metadata) for item in batch.items
    )
    return AgentEnvelopeRender(
        content=content,
        body_content=source.content,
        managed_prefix="".join(member.managed_prefix for member in members),
        managed_suffix="".join(member.managed_suffix for member in members),
    )


def _option(batch: ClaimedBatch, name: str, default=None):
    for item in reversed(batch.items):
        if name in item.delivery_options:
            return item.delivery_options[name]
    return default


def _attachments_for(batch) -> list[dict]:
    """Collect every FIFO attachment in the batch's preserved input order."""
    return [
        attachment
        for item in batch.items
        for attachment in (item.delivery_options.get("attachments") or [])
    ]


def _project_attachments(batch, *, thread_row, target):
    attachments = _attachments_for(batch)
    if not attachments:
        return (), None
    from core.media_delivery import build_delivery_projection

    public, projection = build_delivery_projection(
        attachments,
        owner_id=str(thread_row["user_id"] or ""),
        thread_id=batch.thread_id,
        target=target,
        trust_descriptor_digests=True,
    )
    return tuple(public), projection


async def _project_attachments_async(batch, *, thread_row, target):
    """Validate uploads on the DB pool and fit their pixels outside it."""
    attachments = _attachments_for(batch)
    if not attachments:
        return (), None
    from core.media_delivery import build_delivery_projection_async

    public, projection = await build_delivery_projection_async(
        attachments,
        owner_id=str(thread_row["user_id"] or ""),
        thread_id=batch.thread_id,
        target=target,
        trust_descriptor_digests=True,
    )
    return tuple(public), projection


def _projection_identity(
    rendered: RenderedInputBatch,
    *,
    content: str,
    media_projection,
) -> str:
    payload = {
        "batch": rendered.identity,
        "content": content,
        "media_digests": (
            media_projection.digests if media_projection is not None else ()
        ),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "input-projection:" + hashlib.sha256(canonical.encode()).hexdigest()


def _render_input_projection(
    batch: ClaimedBatch,
    *,
    attachments,
    media_projection,
) -> InputProviderProjection:
    """Build the provider projection after attachment work has completed."""
    rendered = render_claimed_batch(batch)
    source = render_claimed_batch(batch, frame_agent_inputs=False)
    envelope = _batch_agent_envelope(batch, source, rendered.content)
    return InputProviderProjection(
        _projection_identity(
            rendered,
            content=envelope.content,
            media_projection=media_projection,
        ),
        envelope.content,
        source.content,
        envelope,
        attachments,
        media_projection,
        batch.items,
    )


def project_claimed_input(
    db,
    batch: ClaimedBatch,
    *,
    thread_row,
    target,
) -> InputProviderProjection:
    """Render one claimed batch exactly once for preflight and dispatch."""
    attachments, media_projection = _project_attachments(
        batch,
        thread_row=thread_row,
        target=target,
    )
    return _render_input_projection(
        batch, attachments=attachments, media_projection=media_projection,
    )


def _load_input_projection(batch, thread_row, target):
    from infra.database import get_db

    connection = get_db()
    try:
        return project_claimed_input(
            connection, batch, thread_row=thread_row, target=target,
        )
    finally:
        connection.close()


async def _load_input_projection_async(batch, thread_row, target):
    """Render input without holding a DB worker through image normalization."""
    attachments, media_projection = await _project_attachments_async(
        batch, thread_row=thread_row, target=target,
    )
    return _render_input_projection(
        batch, attachments=attachments, media_projection=media_projection,
    )


async def prepare_live_input(batch, live, thread_row, target):
    """Admit a fitting Send prefix; return the same unstarted suffix to the FIFO."""
    from infra.input_writes import release_batch_before_write
    from infra.preflight_attempt import live_input_fits

    try:
        for prefix in safe_input_prefixes(batch):
            projection = await _load_input_projection_async(prefix, thread_row, target)
            if await live_input_fits(prefix, live, thread_row, target, projection):
                break
        else:
            prefix = None
    except BaseException:
        await run_in_db(release_batch_before_write, batch)
        raise
    if prefix is None:
        await run_in_db(release_batch_before_write, batch)
        return None
    if len(prefix.items) < len(batch.items):
        suffix = replace(batch, items=batch.items[len(prefix.items):])
        await run_in_db(release_batch_before_write, suffix)
    return prefix, projection


def _native_prepared_context(provider: str, prepared):
    if provider != "native" or prepared is None:
        return prepared
    from core.turn_context import NativePreparedContext

    payload = prepared.rendered.payload
    if not isinstance(payload, NativePreparedContext):
        raise TypeError("native preflight did not produce NativePreparedContext")
    return replace(payload, fit_ledger=prepared.fit_ledger)


async def _render_claimed_selection(
    *, batch, thread_row, target, provider, projection, selection,
):
    from infra.send_preflight import _destination_attempt

    attempt = _destination_attempt(
        thread_id=batch.thread_id,
        thread_row=thread_row,
        provider=provider,
        target=target,
        current_user_content=projection.content,
        current_user_source_content=projection.source_content,
        agent_envelope=projection.agent_envelope,
        attachments=list(projection.attachments),
        media_projection=projection.media_projection,
        input_identity=batch.turn_id,
    )
    return (await attempt.cold(selection, trigger_reason=None)).prepared_context


async def prepare_claimed_batch(
    batch: ClaimedBatch,
    *,
    thread_row: dict,
    target,
) -> PreparedBatch:
    """Render and preflight the exact claimed batch once."""
    import infra.runtime_truth as runtime_truth
    import infra.send_preflight as send_preflight
    from infra.send_dispatch import provider_for

    truth = await runtime_truth.read_runtime_truth(
        batch.thread_id, thread_row, target=target,
    )
    if not truth.get("addressable", True):
        detail = str(truth.get("addressability_error") or "unavailable")
        raise SendRejected(409, detail)
    provider, is_subagent = provider_for(str(truth["execution_class"]))
    projection = await _load_input_projection_async(batch, thread_row, target)
    async def preflight(current_truth, current_thread_row):
        return await send_preflight.context_preflight(
            thread_id=batch.thread_id,
            thread_row=current_thread_row,
            provider=provider,
            target=target,
            runtime_truth=current_truth,
            current_user_content=projection.content,
            current_user_source_content=projection.source_content,
            agent_envelope=projection.agent_envelope,
            attachments=list(projection.attachments),
            media_projection=projection.media_projection,
            exclude_message_id=None,
            preadmitted_turn_id=batch.turn_id,
            allow_provider_resume=True,
        )

    result = await preflight(truth, thread_row)

    async def refresh_prepared_context(selection=None):
        nonlocal target
        from infra.queries import load_thread_row
        from llm.resolution import resolve_execution_target

        current_thread_row = await run_in_db(load_thread_row, batch.thread_id)
        if not current_thread_row:
            raise SendRejected(404, "thread does not exist")
        target = await run_in_db(resolve_execution_target, target.catalog_id)
        if selection is not None:
            return _native_prepared_context(
                provider, await _render_claimed_selection(
                    batch=batch, thread_row=current_thread_row, target=target,
                    provider=provider, projection=projection, selection=selection,
                ),
            )
        refreshed = await runtime_truth.read_runtime_truth(
            batch.thread_id, current_thread_row, target=target,
        )
        return _native_prepared_context(
            provider,
            (await preflight(refreshed, current_thread_row)).get("prepared_context"),
        )

    return PreparedBatch(
        thread_row,
        target,
        provider,
        is_subagent,
        projection.content,
        projection.agent_envelope,
        list(projection.attachments),
        projection.media_projection,
        _native_prepared_context(provider, result.get("prepared_context")),
        refresh_prepared_context,
        truth,
    )
