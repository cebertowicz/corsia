"""Immutable Claude Code context and provider-bound payload types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from core.continuity_accounting import ContinuityAccounting
from core.effective_context import EffectiveContextSnapshot
from history.types import HistoryBoundary

if TYPE_CHECKING:
    from core.installed_context import InstalledDelivery


@dataclass(frozen=True)
class CCContextReconstruction:
    seed_text: str
    persisted_seed_text: str
    snapshot: EffectiveContextSnapshot
    # Private InstalledDelivery the dispatch retains; never published.
    installed: InstalledDelivery | None = field(
        default=None, compare=False, repr=False,
    )

    @property
    def snapshot_id(self) -> str:
        return self.snapshot.snapshot_id

    @property
    def content_hash(self) -> str:
        return self.snapshot.content_hash


@dataclass(frozen=True)
class CCUserFrameReconstruction:
    frame_text: str
    turn_text: str
    snapshot: EffectiveContextSnapshot
    continuity_accounting: ContinuityAccounting

    @property
    def snapshot_id(self) -> str:
        return self.snapshot.snapshot_id

    @property
    def content_hash(self) -> str:
        return self.snapshot.content_hash


@dataclass(frozen=True)
class CCTranscriptReconstruction:
    path: str
    cc_session_id: str
    content_hash: str
    line_count: int
    format_version: int | None = None
    start_exclusive: HistoryBoundary | None = None
    sealed_boundary: HistoryBoundary | None = None
    full_raw_replay_available: bool = True
    snapshot_history: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class CCPreparedTranscript:
    """Frozen Claude resume JSONL plus its one-time install metadata."""

    reconstruction: CCTranscriptReconstruction
    content: str
    transcript_root: str
    project: str
    display: str
    timestamp: str


@dataclass(frozen=True)
class CCLiveHandleFingerprint:
    """In-process identity of the exact live provider handle preflight measured."""

    corsia_thread_id: str
    guard_identity: int
    stream_identity: int
    provider_session_id: str | None
    pid: int | None
    catalog_model_id: str | None
    transport_model: str | None
    runtime_context_epoch: int


@dataclass(frozen=True)
class CCProducerHandleFingerprint:
    """Stable identity of one durable CC provider context."""

    corsia_thread_id: str
    provider_session_id: str | None
    catalog_model_id: str
    transport_model: str | None
    runtime_context_epoch: int
    context_snapshot_id: str | None


@dataclass(frozen=True)
class CCPreparedDestination:
    """Exact provider-bound Claude payload frozen by context preflight."""

    thread_id: str
    mode: str
    target_catalog_id: str
    current_user_text: str
    working_dir: str
    cc_session_id: str
    prior_cc_session_id: str | None
    is_subagent: bool
    history: tuple[dict[str, Any], ...]
    checkpoint_text: str | None
    seed_reconstruction: CCContextReconstruction | None
    frame_reconstruction: CCUserFrameReconstruction
    transcript: CCPreparedTranscript | None
    tool_registry: Any
    tool_definitions: tuple[dict[str, Any], ...]
    live_handle_fingerprint: CCLiveHandleFingerprint | None
    input_tokens: int
    source_tokens: int
    source_section_tokens: dict[str, int]
    fit_section_tokens: dict[str, int]
    launch_identity: object = field(default_factory=object, compare=False, repr=False)

    @property
    def accounting_payload(self) -> dict[str, Any]:
        """Return the exact frozen model-facing CC payload components."""
        return {
            "seed_text": (
                self.seed_reconstruction.seed_text
                if self.seed_reconstruction is not None else None
            ),
            "history_transcript": (
                self.transcript.content if self.transcript is not None else None
            ),
            "current_user_frame": self.frame_reconstruction.turn_text,
            "tool_definitions": list(self.tool_definitions),
        }
