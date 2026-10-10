"""Public composition surface for Claude Code context reconstruction."""

from subagents.cc_context_frame import build_cc_user_frame_reconstruction
from subagents.cc_context_seed import build_cc_context_reconstruction
from subagents.cc_context_sources import collect_context_sources
from subagents.cc_context_types import (
    CCContextReconstruction,
    CCLiveHandleFingerprint,
    CCPreparedDestination,
    CCPreparedTranscript,
    CCProducerHandleFingerprint,
    CCTranscriptReconstruction,
    CCUserFrameReconstruction,
)
from subagents.cc_transcript_projection import cc_snapshot_history
from subagents.cc_transcript_store import (
    _transcript_path_for_cwd,
    install_cc_prepared_transcript,
    prepare_cc_transcript_from_corsia,
    reconstruct_cc_transcript_from_corsia,
)

__all__ = [
    "CCContextReconstruction",
    "CCLiveHandleFingerprint",
    "CCPreparedDestination",
    "CCPreparedTranscript",
    "CCProducerHandleFingerprint",
    "CCTranscriptReconstruction",
    "CCUserFrameReconstruction",
    "build_cc_context_reconstruction",
    "build_cc_user_frame_reconstruction",
    "cc_snapshot_history",
    "collect_context_sources",
    "install_cc_prepared_transcript",
    "prepare_cc_transcript_from_corsia",
    "reconstruct_cc_transcript_from_corsia",
]
