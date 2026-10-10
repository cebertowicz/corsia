"""Consume one preflight-frozen Claude Code destination."""

from __future__ import annotations

import os

from subagents.cc_handle import validate_prepared_live_handle
from subagents.cc_replay_authority import require_cc_replay_authority
from subagents.cc_runtime_errors import CCPreparedDestinationStale
from subagents.cc_runtime_helpers import _proc_is_alive
from subagents.cc_runtime_state import _Live


def _validate_prepared_resume_live(live: _Live, destination) -> None:
    session = live.session
    provider_session_id = (
        getattr(session, "resume_session_id", None)
        or getattr(session, "session_id", None)
    )
    transcript_path = (
        destination.transcript.reconstruction.path
        if destination.transcript is not None
        else None
    )
    if (
        not _proc_is_alive(session)
        or provider_session_id != destination.cc_session_id
        or os.path.abspath(getattr(session, "working_dir", None) or "")
        != os.path.abspath(destination.working_dir)
        or getattr(session, "reconstructed_transcript_path", None) != transcript_path
    ):
        raise CCPreparedDestinationStale(
            "Claude Code provider resume changed after context preflight"
        )


def build_cc_user_frame(
    *,
    thread_id: str,
    text: str,
    live: _Live,
    target,
    prepared_context,
    db=None,
):
    """Return only the exact frame already frozen by preflight."""
    authority = require_cc_replay_authority(
        thread_id,
        text,
        target_catalog_id=target.catalog_id,
        prepared_context=prepared_context,
    )
    destination = authority.destination
    if (
        authority.source == "provider_resume"
        and destination.live_handle_fingerprint is not None
    ):
        validate_prepared_live_handle(
            thread_id,
            live,
            target=target,
            fingerprint=destination.live_handle_fingerprint,
        )
    else:
        _validate_prepared_resume_live(live, destination)
    return destination.frame_reconstruction
