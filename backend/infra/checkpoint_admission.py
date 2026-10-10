"""Pure predicates for admitting one monotonic checkpoint generation."""

from __future__ import annotations

from core.checkpoint_types import CheckpointRecord, CheckpointRequest

# A checkpoint costs a real provider call, so an idle thread has to have
# produced enough new foldable material to be worth folding. This matches the
# additional-foldable-material cadence the Turn contract already uses, so idle
# capture and pressure capture agree on what "enough new work" means.
IDLE_MIN_DELTA_TOKENS = 32_768

# Provider prompt caches (Anthropic 5 min, OpenAI 5-10 min) expire measured
# from the last request's START. An idle fold must start within 4m45s of that
# start to read the prefix warm. CORSIA records no provider-call start or
# duration (llm_usage keeps only created_at at call end and ttft_ms), so the
# timer is anchored on the Turn's ended_at: 4m00s after it leaves 45s of the
# 5-minute budget for the last call's unrecorded generation time.
IDLE_WARM_DELAY_SECONDS = 240


def admits_idle_checkpoint(new_foldable_tokens: int) -> bool:
    """Whether one producer-owned due Turn has enough unfurled material."""
    return new_foldable_tokens >= IDLE_MIN_DELTA_TOKENS


def advances_or_switch_refreshes(
    head: CheckpointRecord | None,
    candidate: CheckpointRecord | CheckpointRequest,
) -> bool:
    """Allow only forward coverage or a model-switch rewrite at the same cut."""
    if head is None or candidate.through_seq > head.through_seq:
        return True
    return bool(
        candidate.trigger == "model_switch"
        and candidate.through_seq == head.through_seq
        and candidate.through_message_id == head.through_message_id
    )


def installed_winner(
    head: CheckpointRecord | None,
    request: CheckpointRequest,
) -> CheckpointRecord | None:
    """Match an installed same-thread/epoch head to the request's predecessor."""
    if head is None or head.previous_generation_id != request.previous_generation_id:
        return None
    identity = (
        "through_seq",
        "through_message_id",
        "trigger",
        "mode",
        "source_harness",
        "source_model",
        "writer_harness",
        "writer_model",
        "originating_turn_id",
    )
    matches = all(
        getattr(head, field) == getattr(request, field)
        for field in identity
    )
    # A cold request does not know its fitted start until its actual writer
    # surface loads. A known start still has to match exactly; warm identity
    # remains unchanged.
    first_matches = head.first_included_seq == request.first_included_seq or (
        request.mode == "cold" and request.first_included_seq is None
    )
    return head if matches and first_matches else None
