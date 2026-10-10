"""Choose one cache-preserving destination payload before provider dispatch."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import infra.preflight_attempt as preflight_attempt
from infra.db_async import run_in_db
from infra.destination_payloads import ExternalPreparedContextStale
from infra.provider_resume import (
    provider_resume_available as _provider_resume_available,
    provider_resume_decision,
)
from infra.send_replay_reads import read_candidates as _read_candidates
from infra.queries import load_thread_row
from infra.send_types import SendRejected
from llm.resolution import ExecutionTarget

if TYPE_CHECKING:
    from core.media_projection import ValidatedMediaProjection

log = logging.getLogger(__name__)


# Provider-resume availability and identity-matched capacity live in
# infra.provider_resume so preview and send cannot drift.

def _result_input_tokens(result) -> int | None:
    prepared = getattr(result, "prepared_context", None)
    rendered = getattr(prepared, "rendered", None)
    value = getattr(rendered, "input_tokens", None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


async def _render_reconstruction(attempt, candidates, exclude_message_id=None):
    # Installed coverage replaces archive; fit cannot make the archive legal.
    if candidates.checkpoint_tail is not None:
        return await attempt.cold(candidates.checkpoint_tail, trigger_reason=None), candidates
    if candidates.exact is None:
        return None, candidates
    return await attempt.cold(candidates.exact, trigger_reason=None), candidates


async def _provider_resume_attempt(
    attempt,
    candidates,
    decision,
    exclude_message_id=None,
):
    retained = decision.retained_tokens or 0
    canonical = None
    if decision.capacity is None:
        exact_result, candidates = await _render_reconstruction(attempt, candidates, exclude_message_id)
        canonical = _result_input_tokens(exact_result)
    result = await attempt.live(
        candidates,
        retained_provider_tokens=retained,
        canonical_input_tokens=canonical,
        trigger_reason=None,
    )
    return result, candidates


def _destination_attempt(
    *,
    thread_id: str,
    thread_row: Any,
    provider: str,
    target: ExecutionTarget,
    current_user_content: str,
    current_user_source_content: str,
    agent_envelope,
    attachments,
    media_projection,
    input_identity: str,
):
    return preflight_attempt.DestinationAttempt(
        thread_id=thread_id,
        thread_row=thread_row,
        provider=provider,
        target=target,
        current_user_content=current_user_content,
        current_user_source_content=current_user_source_content,
        agent_envelope=agent_envelope,
        attachments=attachments,
        media_projection=media_projection,
        input_identity=input_identity,
    )


def _no_safe_replay() -> SendRejected:
    return SendRejected(
        409,
        "No exact replay or checkpoint-covered tail is available for this turn.",
        metadata={"cause": "context_replay_unavailable"},
    )


async def _installed_if_fitting(attempt, candidates):
    selection = candidates.checkpoint_tail
    if selection is None:
        return None
    result = await attempt.cold(selection, trigger_reason=None)
    return result if result.status == "allowed" else None


async def context_preflight(
    *,
    thread_id: str,
    thread_row: Any,
    provider: str,
    target: ExecutionTarget,
    runtime_truth: dict,
    current_user_content: str,
    current_user_source_content: str | None = None,
    agent_envelope=None,
    attachments: list | None = None,
    media_projection: "ValidatedMediaProjection | None" = None,
    exclude_message_id: str | None = None,
    preadmitted_turn_id: str | None = None,
    allow_provider_resume: bool = True,
) -> dict:
    """Freeze one destination; the bound live Turn owns checkpoint decisions."""
    source = current_user_content if current_user_source_content is None else (
        current_user_source_content
    )
    attempt = _destination_attempt(
        thread_id=thread_id,
        thread_row=thread_row,
        provider=provider,
        target=target,
        current_user_content=current_user_content,
        current_user_source_content=source,
        agent_envelope=agent_envelope,
        attachments=attachments,
        media_projection=media_projection,
        input_identity=(
            exclude_message_id
            or preadmitted_turn_id
            or f"next-input:{thread_id}"
        ),
    )
    candidates = await run_in_db(
        _read_candidates, thread_id, exclude_message_id,
    )
    decision = await run_in_db(
        provider_resume_decision,
        thread_id,
        provider=provider,
        thread_row=thread_row,
        target=target,
        runtime_truth=runtime_truth,
    )
    if allow_provider_resume and decision.available:
        try:
            result, candidates = await _provider_resume_attempt(
                attempt, candidates, decision, exclude_message_id,
            )
        except ExternalPreparedContextStale as error:
            # A rejected handle is not a provider-write failure. The existing
            # admission owner can consume a fresh cold destination for this input.
            log.info("%s cannot resume its current provider handle: %s", thread_id, error)
            current_row = await run_in_db(load_thread_row, thread_id)
            if not current_row:
                raise SendRejected(404, "thread does not exist") from error
            attempt = replace(attempt, thread_row=current_row)
            candidates = await run_in_db(_read_candidates, thread_id, exclude_message_id)
        else:
            if result.status == "allowed":
                return result.to_dict()
            installed = await _installed_if_fitting(attempt, candidates)
            return (installed or result).to_dict()
    exact, candidates = await _render_reconstruction(attempt, candidates, exclude_message_id)
    if exact is None:
        raise _no_safe_replay()
    return exact.to_dict()
