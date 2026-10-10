from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from secrets import compare_digest

from core.continuity_accounting import ContinuityAccounting
from core.turn_core import TurnIdentity
from infra.provider_usage import TokenUsage
from llm.resolution import Harness
from subagents.live_registry import LiveBase, LiveRegistry

#: One CC live-frame broadcast. Defined here, with the handle whose events it
#: carries, because four CC modules each declared this identical alias and a
#: type that means the same thing in four places is four places to change it.
Broadcast = Callable[[str, dict], None]


@dataclass(frozen=True)
class PendingProcessExit:
    identity: TurnIdentity
    code: int | None
    stderr: str | BaseException


@dataclass
class _Live(LiveBase):
    """In-memory handle for one live Claude Code subprocess.

    The shared `LiveBase` carries `session` / `parent_session_id` / `name` plus
    the turn-streaming buffers; CC adds its own fields below. (`session` is a
    `CCStreamSession` at runtime; typed loosely as `object | None` on the base.)
    """

    seed_file: str = ""
    # Absolute config/transcript root bound to this exact CC invocation.
    claude_dir: str | None = None
    model: str | None = None
    catalog_model_id: str | None = None
    harness: Harness = "claude-code"
    runtime_context_epoch: int = 0
    context_window: int | None = None
    discovered_context_window: int | None = None
    expected_reported_model: str | None = None
    provider_reported_model: str | None = None
    context_snapshot_id: str | None = None
    context_snapshot: dict | None = None
    source_artifacts: list[dict] | None = None
    continuity_accounting: ContinuityAccounting | None = None
    # Latest verified final ``message_delta`` measurement for this Turn. Each
    # provider call replaces the previous value; terminal handling consumes it.
    # This is the CONTEXT OCCUPANCY sample -- how full the window is now.
    last_call_usage: dict | None = None
    # Accumulated token volume for this Turn: every provider call added
    # together. A Turn may contain many calls, so the ledger records this
    # rather than the final sample.
    #
    # Lifecycle, stated once because a stale accumulator would bill one Turn's
    # tokens to the next: it is RESET in ``cc_accept._bind_turn``, which every
    # newly accepted durable Turn passes through, and CONSUMED once at the
    # terminal by ``cc_runtime_events._consume_turn_volume``. A Turn that ends
    # without reaching a terminal -- interrupt, process exit, supersession --
    # therefore leaves a value behind that the next bind discards rather than bills.
    # It is deliberately NOT cleared at compaction boundaries: compaction
    # invalidates occupancy, but tokens already spent were still spent.
    turn_usage: TokenUsage | None = None
    #: This Turn's calls whose measured identity never reached
    #: ``final_call_usage``, each given its own ordinal defect key.
    #: Reset with ``turn_usage`` at bind.
    unreceipted_calls: int = 0
    # Typed provider evidence travels with ``thinking_buf`` into the next
    # persisted Assistant Step and is cleared at the same boundary.
    reasoning_provenance: dict | None = None
    # Claude's message.id for that pending reasoning, for a flushed Step.
    reasoning_provider_message_id: str | None = None
    user_driving: bool = False
    provider_context_untrusted: bool = False
    # Measured occupancy immediately before an acknowledged model retarget.
    # The first destination turn consumes and verifies this floor before the
    # provider-local continuation is trusted for another turn.
    post_retarget_context_usage_floor: int | float | None = None
    # This turn's input never became durably delivered. It gates *event
    # ordering only* — later provider output would otherwise persist an
    # assistant row ahead of the user row it answers, which is re-queued for
    # delivery. It never blocks a send, a steer, or a parent wake, and it is
    # cleared at terminal so the re-delivery starts clean.
    input_delivery_unproven: bool = False
    # Callable-capability escapes own one redacted marker. The event pump then
    # terminates the exact process, whose exit must not add another marker.
    tool_boundary_violation_recorded: bool = False
    tool_boundary_marker_id: str | None = None
    initialized: bool = False
    resume_rejected: bool = False
    # Non-None only while the feed that writes a native resume's first input
    # owns Claude's verdict on it: None once Claude emits any event, else the
    # deferred process-exit terminal when Claude refused the session unread.
    resume_verdict: asyncio.Future | None = field(default=None, repr=False)
    last_exit_stderr: str = ""
    # One natural process exit remains owned by this exact Turn until its
    # missing tool results and terminal transition are durably committed.
    pending_process_exit: PendingProcessExit | None = None
    process_exit_resolved: bool = False
    synthetic_resume: bool = False
    tool_registry: object | None = None
    #: Stream content-block index -> the provider call whose input it is streaming, held
    #: only from that block's start to its stop.
    preview_blocks: dict[int, str] = field(default_factory=dict)
    # Exact provider-facing schemas frozen at launch preflight. The loopback
    # endpoint serves these directly so later config/registry changes cannot
    # diverge from the payload whose occupancy was measured.
    tool_definitions: tuple[dict, ...] | None = None
    prepared_destination_identity: object | None = field(
        default=None,
        repr=False,
    )
    tool_token: str = ""
    tool_config: str = ""
    # Backend loopback tasks admitted by this exact process authority. Close
    # must cancel and join them before releasing the Turn owner.
    admitted_tool_tasks: set[asyncio.Task] = field(
        default_factory=set,
        repr=False,
    )
    # Monotonic evidence that this Turn admitted tool execution, including a
    # fast task that completed before a later compaction boundary was read.
    tool_execution_admitted: bool = False
    # One loopback execution per provider call. Reset when a new Turn binds.
    claimed_tool_calls: set[str] = field(default_factory=set, repr=False)
    # Adapter-local input/preemption state. Durable product liveness comes from
    # the matching active-turn row, not from this reusable process handle.
    turn_active: bool = False
    # Process-interrupt handshake shared by user preemption and close/quiesce.
    # It is insufficient to classify why the Turn was interrupted.
    interrupt_requested: bool = False
    # Exact Turn whose provider interrupt was authorized by the current
    # ``apply_now`` delivery operation. Generic close/quiesce paths never set
    # this, so their error terminals cannot masquerade as successful steering.
    apply_now_interrupt_turn: TurnIdentity | None = None
    # Set by the ``result`` terminal handler when present — lets the preempt
    # await the in-flight turn's commit instead of polling.
    turn_terminal: object | None = None  # asyncio.Event at runtime
    # Tool settlement is adapter-local: observed CC tool_use owns the call
    # until its canonical result is durable. Preemption waits on this boundary.
    # A loopback request can arrive just before the matching Assistant Step;
    # this single edge wakes it after every call in that Step is durable/mapped.
    tool_steps_ready: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    tools_settled: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    event_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def __post_init__(self) -> None:
        if not self.tool_step_ids:
            self.tools_settled.set()

    def bind_windows(self, target) -> None:
        """Adopt a resolved target's counted and discovered windows together.

        They are one fact about the target (#1753); every site that rebinds a
        handle to a new target sets both, so one can never go stale alone.
        """
        self.context_window = target.context_window
        self.discovered_context_window = target.discovered_context_window


def activate_bound_turn(live: _Live, identity: TurnIdentity) -> bool:
    """Mark one exact bound Turn active without changing its owner."""
    if live.corsia_turn != identity:
        return False
    if live.session is not None:
        live.session.corsia_turn_identity = identity
    if live.turn_active:
        return True
    live.turn_terminal = asyncio.Event()
    live.interrupt_requested = False
    live.apply_now_interrupt_turn = None
    live.turn_active = True
    return True


def settle_resume_verdict(live: _Live, verdict: object) -> bool:
    """Hand Claude's resume verdict to the feed that owns it, if one does."""
    pending = getattr(live, "resume_verdict", None)
    if pending is None or pending.done():
        return False
    pending.set_result(verdict)
    return True


# CC's live-process handles and per-session transition locks.
_LIVE: LiveRegistry[_Live] = LiveRegistry()

_SPAWN_LOCK_TIMEOUT_S: float = 30.0
_CLOSE_LIVE_TIMEOUT_S: float = 35.0

# Dual-accept window for rotated tool-bridge tokens. A live-handle transition
# (quiesce/preempt/close then rebuild) revokes the current token and installs a
# fresh one on the successor. A tool call already issued under the previous
# token would otherwise land mid-transition as an undescriptive 401 and be
# lost (issue #825). Each session remembers exactly its immediately-previous
# token for this window so the bridge can keep authenticating those calls and
# can name later rejections precisely.
_TOOL_TOKEN_GRACE_WINDOW_S: float = 30.0

# Canonical wire contract: the exact 401 detail the bridge sends when a call
# presents a rotated-out token. The MCP client shim keys its single bounded
# retry on this string; it keeps a mirrored literal because it runs standalone
# in the CC subprocess and cannot import backend modules
# (``subagents/corsia_mcp_server.py``, pinned equal by test).
TOOL_TOKEN_ROTATED_DETAIL = "Claude Code tool token rotated"


@dataclass(frozen=True)
class _RotatedToolToken:
    """One session's immediately-previous tool token and its expiry."""

    token: str
    expires_at: float


# session_id -> the single remembered predecessor token. Bounded by design:
# one record per session, overwritten on every rotation (so a grandparent
# token dies the moment its successor rotates); expired entries are swept on
# every read and write; a terminal close purges the session's record.
_TOOL_TOKEN_GRACE: dict[str, _RotatedToolToken] = {}


def _sweep_expired_tool_tokens(current: float) -> None:
    """Drop every rotation record whose dual-accept window has closed."""
    for stale_id in [
        sid
        for sid, record in _TOOL_TOKEN_GRACE.items()
        if record.expires_at <= current
    ]:
        del _TOOL_TOKEN_GRACE[stale_id]


def record_tool_token_rotation(
    thread_id: str,
    token: str,
    *,
    now: float | None = None,
) -> None:
    """Remember ``token`` as ``session_id``'s immediately-previous tool token.

    Called at the exact moment a token stops being current (revocation or
    in-place reconfiguration). Empty tokens are ignored; an existing record is
    overwritten so at most one predecessor is ever accepted.
    """
    if not thread_id or not token:
        return
    current = time.monotonic() if now is None else now
    _sweep_expired_tool_tokens(current)
    _TOOL_TOKEN_GRACE[thread_id] = _RotatedToolToken(
        token,
        current + _TOOL_TOKEN_GRACE_WINDOW_S,
    )


def is_grace_tool_token(
    thread_id: str,
    token: str,
    *,
    now: float | None = None,
) -> bool:
    """Whether ``token`` is the session's predecessor, inside its window.

    Expired records are swept on this read path, so a token past its window
    is indistinguishable from a foreign token.
    """
    current = time.monotonic() if now is None else now
    _sweep_expired_tool_tokens(current)
    record = _TOOL_TOKEN_GRACE.get(thread_id)
    if record is None or not token:
        return False
    return compare_digest(token, record.token)


def session_recently_rotated(
    thread_id: str,
    *,
    now: float | None = None,
) -> bool:
    """Whether this session holds an unexpired tool-token rotation record."""
    _sweep_expired_tool_tokens(time.monotonic() if now is None else now)
    return thread_id in _TOOL_TOKEN_GRACE


def purge_tool_token_grace(thread_id: str) -> None:
    """Forget a session's rotation record at its terminal close.

    Only genuinely-terminal paths (user stop, destructive retirement) call
    this; replacement transitions must keep the record so the successor can
    dual-accept the predecessor's in-flight calls.
    """
    _TOOL_TOKEN_GRACE.pop(thread_id, None)


def reset_tool_token_grace() -> None:
    """Clear rotation records for isolated test fixtures."""
    _TOOL_TOKEN_GRACE.clear()
