"""Shared source inventory for Claude seed and live-frame snapshots."""

from __future__ import annotations

from typing import Any

from core import dynamic_context_registry
from core.effective_context import ContextSourceEntry
from history.types import HistoryBoundary
from subagents.cc_context_types import CCTranscriptReconstruction


def collect_context_sources(**kwargs):
    """Resolve the canonical registry at call time, not module-import time."""
    return dynamic_context_registry.collect_context_sources(**kwargs)


def _boundary_payload(boundary: HistoryBoundary | None) -> dict[str, Any] | None:
    if boundary is None:
        return None
    return {
        "thread_id": boundary.thread_id,
        "history_epoch": boundary.history_epoch,
        "seq": boundary.seq,
        "message_id": boundary.message_id,
    }


def transcript_metadata(
    transcript: CCTranscriptReconstruction,
) -> dict[str, Any]:
    return {
        "cc_session_id": transcript.cc_session_id,
        "content_hash": transcript.content_hash,
        "line_count": transcript.line_count,
        "format_version": transcript.format_version,
        "start_exclusive": _boundary_payload(transcript.start_exclusive),
        "sealed_boundary": _boundary_payload(transcript.sealed_boundary),
        "full_raw_replay_available": transcript.full_raw_replay_available,
    }


def append_replay_sources(
    sources: list,
    provider_payload: dict[str, Any],
    *,
    thread_id: str,
    checkpoint_text: str | None,
    reconstructed_transcript: CCTranscriptReconstruction | None,
    checkpoint_reason: str,
) -> None:
    """Append the exact checkpoint and transcript facts used by this payload."""
    if checkpoint_text:
        sources.append(ContextSourceEntry(
            source_id="history:portable-checkpoint",
            origin_section="history",
            label="Portable checkpoint",
            stable_reference=f"thread://{thread_id}/portable-checkpoint",
            freshness="fresh",
            inclusion="included",
            reason=checkpoint_reason,
            owner_surface="portable-checkpoint",
            prompt_content=checkpoint_text,
        ))
        provider_payload["checkpoint_text"] = checkpoint_text
    if reconstructed_transcript is None:
        return
    metadata = transcript_metadata(reconstructed_transcript)
    sources.append(ContextSourceEntry(
        source_id="cc-reconstructed-transcript",
        origin_section="history",
        label="Claude Code reconstructed resume JSONL",
        stable_reference=reconstructed_transcript.path,
        freshness="fresh",
        inclusion="included",
        reason="Claude Code resume transcript regenerated from CORSIA DB history",
        owner_surface="cc-runtime",
        metadata=metadata,
    ))
    provider_payload["cc_reconstructed_transcript"] = {
        "path": reconstructed_transcript.path,
        **metadata,
    }
