"""One prepared-payload authority for every Claude Code provider call.

Thread role still controls framing and lifecycle. It never chooses history.
A trusted live destination keeps the provider transcript authoritative; every
cold destination consumes the exact CORSIA replay frozen by preflight.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from core.destination_fit import require_provider_call_fit
from subagents.cc_context_types import CCPreparedDestination
from subagents.cc_runtime_errors import CCPreparedDestinationStale


@dataclass(frozen=True, slots=True)
class CCReplayAuthority:
    """The transcript source already selected and frozen by preflight."""

    source: Literal["provider_resume", "exact_replay"]
    destination: Any
    replay: Any | None
    checkpoint_text: str | None = None


def require_cc_provider_fit(prepared_context: Any, target: Any) -> None:
    """Consume the renderer-owned fit ledger at the CC runtime boundary."""
    rendered = getattr(prepared_context, "rendered", None)
    require_provider_call_fit(
        getattr(prepared_context, "fit_ledger", None),
        harness=target.harness,
        target_catalog_id=target.catalog_id,
        accounting_payload=getattr(rendered, "accounting_payload", None),
        input_identity=getattr(rendered, "fit_input_identity", "") or "",
        current_input=getattr(rendered, "fit_current_input", None),
    )


def _prepared_destination(
    thread_id: str,
    current_user_text: str,
    *,
    target_catalog_id: str,
    prepared_context: Any,
) -> Any:
    if prepared_context is None:
        raise ValueError("Claude Code provider call requires a prepared context")
    destination = getattr(
        getattr(prepared_context, "rendered", None),
        "payload",
        None,
    )
    if not isinstance(destination, CCPreparedDestination):
        raise ValueError(
            "Claude Code dispatch requires its exact rendered destination payload"
        )
    if (
        destination.thread_id != thread_id
        or destination.current_user_text != current_user_text
        or destination.target_catalog_id != target_catalog_id
        or prepared_context.target_catalog_id != target_catalog_id
        or destination.mode != prepared_context.mode
    ):
        raise ValueError(
            "Claude Code prepared destination does not match this provider boundary"
        )
    return destination


def _validate_resume_authority(prepared_context: Any, destination: Any) -> None:
    if (
        prepared_context.replay is not None
        or destination.transcript is not None
        or not destination.cc_session_id
    ):
        raise ValueError("Claude Code provider resume carries replay material")
    runtime_epoch = prepared_context.runtime_context_epoch
    handle = destination.live_handle_fingerprint
    if handle is not None and handle.runtime_context_epoch != runtime_epoch:
        raise CCPreparedDestinationStale(
            "Claude Code provider process belongs to an older runtime context epoch"
        )


def _cold_replay(
    thread_id: str,
    prepared_context: Any,
    destination: Any,
) -> tuple[Any, str | None]:
    replay = prepared_context.replay
    if (
        replay is None
        or (
            "exact_replay" if replay.mode == "exact" else replay.mode
        ) != prepared_context.mode
        or destination.live_handle_fingerprint is not None
        or destination.seed_reconstruction is None
    ):
        raise ValueError("Claude Code cold destination is incomplete")
    if (
        replay.tail.thread_id != thread_id
        or replay.tail.history_epoch != prepared_context.history_epoch
        or replay.history_epoch != prepared_context.history_epoch
        or replay.sealed_boundary != prepared_context.sealed_boundary
    ):
        raise ValueError(
            "Claude Code prepared replay does not match the target thread state"
        )
    checkpoint_text = None
    if replay.mode == "checkpoint_tail":
        from core.context_replay import checkpoint_prompt

        checkpoint_text = checkpoint_prompt(replay)
        if not checkpoint_text:
            raise ValueError(
                "Claude Code checkpoint-tail replay has no installed checkpoint"
            )
    return replay, checkpoint_text


def require_cc_replay_authority(
    thread_id: str,
    current_user_text: str,
    *,
    target_catalog_id: str,
    prepared_context: Any,
) -> CCReplayAuthority:
    """Return the one transcript authority selected by prepared destination."""
    destination = _prepared_destination(
        thread_id,
        current_user_text,
        target_catalog_id=target_catalog_id,
        prepared_context=prepared_context,
    )
    if destination.mode == "provider_resume":
        _validate_resume_authority(prepared_context, destination)
        return CCReplayAuthority("provider_resume", destination, None)
    replay, checkpoint_text = _cold_replay(
        thread_id,
        prepared_context,
        destination,
    )
    return CCReplayAuthority(
        "exact_replay",
        destination,
        replay,
        checkpoint_text,
    )
