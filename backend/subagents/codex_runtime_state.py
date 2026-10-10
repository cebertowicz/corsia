from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field

from core.continuity_accounting import ContinuityAccounting
from llm.resolution import Harness
from infra.context_receipts import (
    ContextReceiptIdentity,
    producer_handle_fingerprint as opaque_handle_fingerprint,
)
from subagents.live_registry import LiveBase, LiveRegistry


@dataclass(frozen=True)
class CodexHandleFingerprint:
    """One in-process provider handle identity frozen by preflight."""

    codex_thread_id: str
    live_identity: int | None
    session_identity: int | None
    catalog_model_id: str | None = None
    transport_model: str | None = None
    expected_reported_model: str | None = None
    provider_reported_model: str | None = None
    working_dir: str | None = None
    runtime_context_epoch: int = 0


@dataclass(frozen=True)
class CodexProducerHandleFingerprint:
    """Stable identity of one durable Codex provider context."""

    codex_thread_id: str
    catalog_model_id: str
    transport_model: str | None
    working_dir: str | None
    runtime_context_epoch: int
    context_snapshot_id: str | None


@dataclass
class _LiveCodex(LiveBase):
    """In-memory handle for one live Codex turn.

    The shared `LiveBase` carries `session` / `parent_session_id` / `name` plus
    the turn-streaming buffers; Codex adds the resumable `thread_id`. (`session`
    is a `CodexAppServerSession` at runtime; typed loosely as `object | None` on
    the base.)
    """

    codex_thread_id: str = ""
    model: str | None = None
    catalog_model_id: str | None = None
    harness: Harness = "codex"
    runtime_context_epoch: int = 0
    context_window: int | None = None
    discovered_context_window: int | None = None
    expected_reported_model: str | None = None
    provider_reported_model: str | None = None
    context_snapshot_id: str | None = None
    context_snapshot: dict | None = None
    source_artifacts: list[dict] | None = None
    continuity_accounting: ContinuityAccounting | None = None
    thinking_provenance: dict | None = None
    provider_context_untrusted: bool = False
    # Exit owned by compaction retirement: it terminated this process on purpose
    # and is adopting a checkpoint, so the dead-generation owner must not settle
    # the Turn. Set before terminate and cleared on every exit of that retirement.
    exit_owned_by_compaction_retire: bool = False
    # See the Claude Code runtime state: event ordering only, never a gate on
    # sending, steering, or waking a parent.
    input_delivery_unproven: bool = False
    # One redacted marker owns a provider-native action escape. The app-server
    # reader then terminates the process; its exit must not add a second row.
    tool_boundary_violation_recorded: bool = False
    tool_boundary_marker_id: str | None = None
    requires_terminal_identity: bool = False
    tools_settled: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    # App-server notifications can arrive before ``turn/start`` returns. Hold
    # provider-originated output behind this per-turn gate without blocking the
    # JSON-RPC reader that must receive the response which releases it.
    delivery_event_gate: asyncio.Event | None = field(default=None, repr=False)
    delivery_event_tail: asyncio.Task[None] | None = field(
        default=None,
        repr=False,
    )
    delivery_event_epoch: int = 0

    def __post_init__(self) -> None:
        if not self.tool_step_ids:
            self.tools_settled.set()

    def bind_app_server_session(self, session) -> None:
        """Attach the provider session to this live owner's declaration map."""
        self.session = session
        session.declared_tool_step_ids = self.tool_step_ids
        session.received_event_delivery = self.received_event_delivery

    def received_event_delivery(self) -> asyncio.Task[None] | None:
        """Return the serialized delivery of every provider event received so far."""
        return self.delivery_event_tail


# Codex's live-turn registry and per-session transition locks.
_LIVE: LiveRegistry[_LiveCodex] = LiveRegistry()
_DEAD: set[str] = set()
_CODEX_THREADS: dict[str, str] = {}
_APP_SESSIONS: dict[str, object] = {}
_PROVIDER_CONTEXT_UNTRUSTED: set[str] = set()


def finalize_live(thread_id: str, live: _LiveCodex | None = None) -> None:
    """Drop only the exact live generation that owns this session slot."""
    if live is None or _LIVE.get(thread_id) is live:
        _LIVE.pop(thread_id, None)


def dead_generation_ready_for_settlement(
    thread_id: str, *, retiring: _LiveCodex | None = None,
) -> tuple[_LiveCodex, object, int] | None:
    """Observe under the transition lock; an unfinished reader still owns exit.

    A compaction retirement owns the exit it caused until it settles that exact
    generation itself (``retiring``).
    """
    live = _LIVE.get(thread_id)
    session = live.session if live is not None else None
    returncode = getattr(session, "returncode", None)
    if live is None or session is None or returncode is None:
        return None
    if live.exit_owned_by_compaction_retire and live is not retiring:
        return None
    if _APP_SESSIONS.get(thread_id) not in (None, session):
        return None
    reader = getattr(session, "_reader", None)
    if reader is not None and not reader.done():
        return None
    return live, session, returncode


def _session_working_dir(session: object | None) -> str | None:
    value = getattr(session, "working_dir", None)
    return os.path.abspath(value) if isinstance(value, str) else None


def _session_string(session: object | None, name: str) -> str | None:
    value = getattr(session, name, None)
    return value if isinstance(value, str) else None


def _session_epoch(session: object | None) -> int:
    value = getattr(session, "runtime_context_epoch", 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _thread_id(thread_id: str, live: _LiveCodex | None, session) -> str | None:
    value = (
        (live.codex_thread_id if live is not None else None)
        or getattr(session, "codex_thread_id", None)
        or _CODEX_THREADS.get(thread_id)
    )
    return str(value) if value else None


def _live_handle_fingerprint(
    thread_id: str,
    live: _LiveCodex,
) -> CodexHandleFingerprint | None:
    session = live.session
    codex_thread_id = _thread_id(thread_id, live, session)
    if codex_thread_id is None:
        return None
    return CodexHandleFingerprint(
        codex_thread_id=codex_thread_id,
        live_identity=id(live),
        session_identity=id(session) if session is not None else None,
        catalog_model_id=live.catalog_model_id,
        transport_model=live.model,
        expected_reported_model=live.expected_reported_model,
        provider_reported_model=live.provider_reported_model,
        working_dir=_session_working_dir(session),
        runtime_context_epoch=live.runtime_context_epoch,
    )


def _producer_identity(live: _LiveCodex | None, session) -> tuple[str | None, int]:
    if live is not None:
        return live.catalog_model_id, live.runtime_context_epoch
    return _session_string(session, "catalog_model_id"), _session_epoch(session)


def producer_handle_fingerprint(
    thread_id: str,
    live: _LiveCodex | None,
) -> CodexProducerHandleFingerprint | None:
    """Return the complete stable identity of the current producer."""
    session = live.session if live is not None else _APP_SESSIONS.get(thread_id)
    if session is None or getattr(session, "returncode", None) is not None:
        return None
    codex_thread_id = _thread_id(thread_id, live, session)
    catalog_model_id, runtime_context_epoch = _producer_identity(live, session)
    if (
        codex_thread_id is None
        or not catalog_model_id
        or isinstance(runtime_context_epoch, bool)
        or not isinstance(runtime_context_epoch, int)
        or runtime_context_epoch < 0
    ):
        return None
    return CodexProducerHandleFingerprint(
        codex_thread_id=codex_thread_id,
        catalog_model_id=catalog_model_id,
        transport_model=(
            live.model if live is not None else _session_string(session, "model")
        ),
        working_dir=_session_working_dir(session),
        runtime_context_epoch=runtime_context_epoch,
        context_snapshot_id=(
            live.context_snapshot_id
            if live is not None
            else _session_string(session, "context_snapshot_id")
        ),
    )


def current_handle_fingerprint(thread_id: str) -> CodexHandleFingerprint | None:
    """Return the exact current Codex handle identity, if one exists."""
    live = _LIVE.get(thread_id)
    if live is not None:
        fingerprint = _live_handle_fingerprint(thread_id, live)
        if fingerprint is not None:
            return fingerprint
    session = _APP_SESSIONS.get(thread_id)
    codex_thread_id = _thread_id(thread_id, None, session)
    if codex_thread_id is None:
        return None
    return CodexHandleFingerprint(
        codex_thread_id=codex_thread_id,
        live_identity=None,
        session_identity=id(session) if session is not None else None,
        catalog_model_id=_session_string(session, "catalog_model_id"),
        transport_model=_session_string(session, "model"),
        expected_reported_model=_session_string(session, "expected_reported_model"),
        provider_reported_model=_session_string(session, "provider_reported_model"),
        working_dir=_session_working_dir(session),
        runtime_context_epoch=_session_epoch(session),
    )


def provider_context_untrusted(thread_id: str) -> bool:
    """Return whether Codex's provider-local transcript handle is distrusted."""
    return thread_id in _PROVIDER_CONTEXT_UNTRUSTED


def mark_provider_context_untrusted(thread_id: str) -> None:
    """Distrust only the current local process, preserving provider resume."""
    _PROVIDER_CONTEXT_UNTRUSTED.add(thread_id)
    live = _LIVE.get(thread_id)
    if live is not None:
        live.provider_context_untrusted = True


def retire_exited_generation(thread_id: str, live: _LiveCodex, session) -> None:
    """Retire only this exited generation and its matching app session."""
    if live.provider_context_untrusted and _LIVE.get(thread_id) is live:
        mark_provider_context_untrusted(thread_id)
    finalize_live(thread_id, live)
    if _APP_SESSIONS.get(thread_id) is session:
        _APP_SESSIONS.pop(thread_id, None)


def trusted_handle_fingerprint(
    thread_id: str,
) -> CodexProducerHandleFingerprint | None:
    """Return a complete trusted provider handle, never a thread-only fallback."""
    if provider_context_untrusted(thread_id):
        return None
    live = _LIVE.get(thread_id)
    if live is not None and live.provider_context_untrusted:
        return None
    return producer_handle_fingerprint(thread_id, live)


def context_receipt_identity(
    thread_id: str,
    live: _LiveCodex,
) -> ContextReceiptIdentity | None:
    """Freeze the exact Codex producer identity carried by a context receipt."""
    handle = producer_handle_fingerprint(thread_id, live)
    if handle is None:
        return None
    return ContextReceiptIdentity(
        "codex",
        handle.catalog_model_id,
        handle.runtime_context_epoch,
        opaque_handle_fingerprint(handle),
    )
