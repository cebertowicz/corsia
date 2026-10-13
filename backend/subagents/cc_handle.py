"""Boot-scoped Claude Code provider handle ownership (O5)."""

from __future__ import annotations

import logging

from infra.context_receipts import (
    ContextReceiptIdentity,
    producer_handle_fingerprint as opaque_handle_fingerprint,
)
from infra.db_async import run_in_db
from llm.resolution import ExecutionTarget
from subagents.cc_context_types import (
    CCLiveHandleFingerprint,
    CCProducerHandleFingerprint,
)
from subagents.cc_runtime_errors import CCPreparedDestinationStale
from subagents.cc_runtime_helpers import _proc_is_alive
from subagents.cc_runtime_state import Broadcast, _LIVE, _Live


log = logging.getLogger(__name__)


async def steer_restart_pending(
    session_id: str,
    expected_turn_id: str,
    message: str,
) -> bool:
    """Steer one exact live Claude turn with restart control."""
    async with _LIVE.lock(session_id):
        live = _LIVE.get(session_id)
        if (
            live is None or live.corsia_turn is None
            or live.corsia_turn.turn_id != expected_turn_id
        ):
            return False
        feed = getattr(live.session, "feed", None)
        if feed is None:
            return False
        await feed(message)
        return True


def live_handle_fingerprint(
    session_id: str,
    live: _Live,
) -> CCLiveHandleFingerprint:
    session = live.session
    provider_session_id = (
        getattr(session, "resume_session_id", None)
        or getattr(session, "session_id", None)
    )
    pid = getattr(session, "pid", None)
    return CCLiveHandleFingerprint(
        corsia_thread_id=session_id, guard_identity=id(live),
        stream_identity=id(session), provider_session_id=provider_session_id,
        pid=pid if isinstance(pid, int) else None,
        catalog_model_id=live.catalog_model_id,
        transport_model=live.model,
        runtime_context_epoch=getattr(live, "runtime_context_epoch", 0),
    )


def producer_handle_fingerprint(
    session_id: str,
    live: _Live,
) -> CCProducerHandleFingerprint | None:
    session = live.session
    epoch = live.runtime_context_epoch
    provider_session_id = (
        getattr(session, "resume_session_id", None)
        or getattr(session, "session_id", None)
    )
    if (
        not provider_session_id or not live.catalog_model_id
        or isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0
    ):
        return None
    return CCProducerHandleFingerprint(
        corsia_thread_id=session_id,
        provider_session_id=provider_session_id,
        catalog_model_id=live.catalog_model_id,
        transport_model=live.model,
        runtime_context_epoch=epoch,
        context_snapshot_id=live.context_snapshot_id,
    )


def trusted_handle_fingerprint(session_id: str):
    """Return a complete trusted live handle identity, never a thread fallback."""
    live = _LIVE.get(session_id)
    epoch = getattr(live, "runtime_context_epoch", None)
    if (
        live is None or not _proc_is_alive(live.session)
        or live.provider_context_untrusted or not live.catalog_model_id
        or isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0
    ):
        return None
    return producer_handle_fingerprint(session_id, live)


def context_receipt_identity(
    session_id: str,
    live: _Live,
) -> ContextReceiptIdentity | None:
    """Freeze the exact CC producer identity carried by a context receipt."""
    handle = producer_handle_fingerprint(session_id, live)
    if handle is None:
        return None
    return ContextReceiptIdentity(
        "claude-code",
        handle.catalog_model_id,
        handle.runtime_context_epoch,
        opaque_handle_fingerprint(handle),
    )


async def capture_retirement_owner(session_id: str):
    """Freeze the exact reusable CC owner under its transition lock."""
    async with _LIVE.lock(session_id):
        live = _LIVE.get(session_id)
        return None if live is None else live_handle_fingerprint(session_id, live)


async def retire_if_current(
    session_id: str,
    *,
    expected_owner,
    broadcast: Broadcast | None = None,
    operation_id: str | None = None,
) -> bool:
    """Retire only the exact CC owner captured before a context commit."""
    from subagents.cc_shutdown import retire_live, retire_provider_session

    async with _LIVE.lock(session_id):
        live = _LIVE.get(session_id)
        if live is None:
            if expected_owner is not None:
                log.info(
                    "model switch source runtime branch=owner_gone thread_id=%s operation_id=%s",
                    session_id, operation_id or "unknown",
                )
            await run_in_db(retire_provider_session, session_id)
            return True
        if expected_owner is None or live_handle_fingerprint(session_id, live) != expected_owner:
            log.warning(
                "model switch source runtime branch=owner_changed thread_id=%s operation_id=%s",
                session_id, operation_id or "unknown",
            )
            return False
        if not await retire_live(session_id, broadcast):
            log.warning(
                "model switch source runtime branch=retire_failed thread_id=%s operation_id=%s",
                session_id, operation_id or "unknown",
            )
            return False
        await run_in_db(retire_provider_session, session_id)
        return True


def _retarget_fields(
    live: _Live,
    target: ExecutionTarget,
    runtime_context_epoch: int,
    expected_context_usage_floor: int | float | None,
) -> None:
    live.model = target.transport_model
    live.catalog_model_id = target.catalog_id
    live.harness = target.harness
    live.bind_windows(target)
    live.expected_reported_model = target.expected_reported_model
    live.provider_reported_model = None
    live.runtime_context_epoch = runtime_context_epoch
    valid_floor = (
        not isinstance(expected_context_usage_floor, bool)
        and isinstance(expected_context_usage_floor, (int, float))
        and expected_context_usage_floor >= 0
    )
    live.post_retarget_context_usage_floor = (
        expected_context_usage_floor if valid_floor else None
    )


async def retarget_if_current(
    session_id: str,
    *,
    expected_owner,
    target: ExecutionTarget,
    runtime_context_epoch: int,
    expected_context_usage_floor: int | float | None = None,
) -> bool:
    """Retarget one captured CC process and arm its continuity check."""
    if target.harness != "claude-code":
        raise ValueError("Claude Code continuation cannot target another harness")
    async with _LIVE.lock(session_id):
        live = _LIVE.get(session_id)
        if (
            expected_owner is None or live is None
            or live_handle_fingerprint(session_id, live) != expected_owner
        ):
            return False
        set_model = getattr(live.session, "set_model", None)
        if not callable(set_model):
            return False
        invocation = getattr(live.session, "invocation", None)
        provider_session_id = expected_owner.provider_session_id
        if not callable(invocation) or not provider_session_id:
            raise RuntimeError(
                "Claude Code retarget lacks its durable invocation identity"
            )
        await set_model(target.transport_model)
        from registry.service import register_cc_session

        register_cc_session(
            session_id,
            provider_session_id,
            cwd=getattr(live.session, "working_dir", None),
            invocation=invocation(),
        )
        _retarget_fields(
            live, target, runtime_context_epoch, expected_context_usage_floor
        )
        return True


def validate_prepared_live_handle(
    session_id: str,
    live: _Live,
    *,
    target: ExecutionTarget,
    fingerprint: CCLiveHandleFingerprint,
) -> None:
    current = _LIVE.get(session_id)
    if (
        current is not live or not _proc_is_alive(live.session)
        or live.provider_context_untrusted
        or live_handle_fingerprint(session_id, live) != fingerprint
    ):
        raise CCPreparedDestinationStale(
            "Claude Code live handle changed after context preflight"
        )
    if live.catalog_model_id not in (None, target.catalog_id):
        raise CCPreparedDestinationStale(
            "Claude Code live handle model changed after context preflight"
        )
    if live.model not in (None, target.transport_model):
        raise CCPreparedDestinationStale(
            "Claude Code live handle selector changed after context preflight"
        )
