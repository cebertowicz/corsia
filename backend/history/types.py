"""Value types shared by transcript projection and its renderers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


PORTABLE_HISTORY_FORMAT_VERSION = 1


class PortableHistoryError(RuntimeError):
    """Durable transcript state cannot be projected without guessing."""


class PortableHistoryUnavailableError(PortableHistoryError):
    """The requested exact range contains destructively retained content."""


@dataclass(frozen=True)
class HistoryBoundary:
    thread_id: str
    history_epoch: int
    seq: int
    message_id: str

    @classmethod
    def of_checkpoint(cls, record: Any) -> "HistoryBoundary":
        """Read the exact cut one checkpoint record covers through."""
        return cls(
            record.thread_id,
            record.history_epoch,
            record.through_seq,
            record.through_message_id,
        )


@dataclass(frozen=True)
class DurableBoundaryMessage:
    """The shared harness-neutral view of one projected durable row."""

    role: str
    content: str
    tool_call_id: str | None = None
    message_id: str | None = None
    assistant_step_id: str | None = None
    tool_call_ids: tuple[str, ...] = ()
    # The harness's own id for an assistant row (Claude's ``message.id``,
    # Codex's agentMessage item id): the name a transcript or rollout the
    # harness wrote itself gives the row.
    provider_message_id: str | None = None


@dataclass(frozen=True)
class PortableMessage:
    message_id: str
    seq: int
    turn_id: str | None
    role: str
    content: str
    created_at: str
    assistant_step_id: str | None = None
    tool_calls: tuple[dict[str, Any], ...] = ()
    tool_call_id: str | None = None
    tool_name: str | None = None
    is_error: bool = False
    synthetic_tool_result: bool = False
    synthetic_reason: str | None = None
    reasoning_text: str | None = None
    reasoning_provenance: dict[str, Any] = field(default_factory=dict)
    artifact_references: tuple[dict[str, Any], ...] = ()
    inbound_source: dict[str, Any] = field(default_factory=dict)
    content_sha256: str | None = None
    content_byte_count: int | None = None
    media_metadata: dict[str, Any] | None = None
    provider_message_id: str | None = None

    @property
    def reasoning_content(self) -> str | None:
        if (
            self.reasoning_text
            and self.reasoning_provenance.get("representation_kind")
            == "reasoning_content"
            and self.reasoning_provenance.get("source_harness") != "unknown"
            and self.reasoning_provenance.get("source_provider") != "unknown"
        ):
            return self.reasoning_text
        return None


@dataclass(frozen=True)
class PortableTurn:
    """One Turn, or one contiguous Assistant-Step-aligned fragment of a Turn.

    A checkpoint boundary may land inside a Turn (see
    :func:`turn_interior_cut_points`), which projects that Turn as two
    fragments across two selections. The Turn record itself is never split:
    ``turn_id`` stays the same on both fragments, and
    ``includes_turn_start`` / ``includes_turn_end`` say which end of the Turn
    this projection holds. Both are true for a whole Turn.

    These are deliberately not named ``completes_turn``:
    :class:`CheckpointFoldUnit` already uses that name for a different
    question — whether a fold unit ends its *projected* Turn and so carries a
    commit-legal boundary.
    """

    turn_id: str | None
    status: str
    messages: tuple[PortableMessage, ...]
    boundary: HistoryBoundary
    legacy_reason: str | None = None
    includes_turn_start: bool = True
    includes_turn_end: bool = True


@dataclass(frozen=True)
class CheckpointFoldUnit:
    """One ordered, transient checkpoint-fold input unit.

    ``completes_turn`` means this unit ends its projected :class:`PortableTurn`
    and therefore carries a commit-legal ``boundary``. When the selection was
    rendered through an interior cut point that Turn is a fragment, so the
    boundary is the interior cut rather than the Turn's own end — which is
    exactly what lets an oversized Turn be committed in pieces.
    """

    turn_id: str | None
    status: str
    legacy_reason: str | None
    ordinal: int
    messages: tuple[PortableMessage, ...]
    completes_turn: bool
    boundary: HistoryBoundary | None


@dataclass(frozen=True)
class AssistantStepBatch:
    """One complete Assistant output plus declared tools and its resume cut."""

    initiating_inputs: tuple[PortableMessage, ...]
    assistant_and_tools: tuple[PortableMessage, ...]
    resume_anchor: HistoryBoundary | None
    end_boundary: HistoryBoundary | None


@dataclass(frozen=True)
class CheckpointFoldFragment:
    """One text-bounded fragment with once-per-message semantic metadata."""

    unit_ordinal: int
    turn_id: str | None
    turn_status: str
    turn_legacy_reason: str | None
    payload_kind: Literal[
        "user_text",
        "assistant_text",
        "assistant_reasoning",
        "tool_arguments",
        "tool_result",
    ]
    message_id: str
    message_seq: int
    message_created_at: str
    role: Literal["user", "assistant", "tool"]
    assistant_step_id: str | None
    tool_call_id: str | None
    tool_name: str | None
    is_error: bool
    synthetic_tool_result: bool
    synthetic_reason: str | None
    reasoning_provenance: dict[str, Any]
    artifact_references: tuple[dict[str, Any], ...]
    inbound_source: dict[str, Any]
    part_index: int
    part_count: int
    text: str
    byte_count: int

    @property
    def part_label(self) -> str:
        return f"part {self.part_index}/{self.part_count}"


@dataclass(frozen=True)
class PortableHistoryProjection:
    """One frozen range of a thread, plus what retention took out of it.

    ``retention_stub_seqs`` names the rows whose tool payload was destructively
    trimmed, and ``full_raw_replay_available`` is the same fact as a flag. Both
    are EVIDENCE, never a veto: a holed projection ranks below an intact
    checkpoint replay (``core/context_replay.py::read_replay_candidates``)
    and declares its holes in band when it is the only replay left
    (``core/context_replay.py::history_loss_notice``). Refusing to render it
    was the last send-path stop — a thread that had been trimmed and whose
    covering checkpoint went bad had no replay at all and could not be sent to.
    """

    format_version: int
    thread_id: str
    history_epoch: int
    start_exclusive: HistoryBoundary | None
    sealed_boundary: HistoryBoundary | None
    turns: tuple[PortableTurn, ...]
    full_raw_replay_available: bool
    retention_stub_seqs: tuple[int, ...] = ()
    # One entry per declared call that had no durable result and projected a
    # missing-result stub, at its owning Assistant Step's seq — so a Step that
    # lost several results repeats that seq and the count stays truthful. Same
    # standing as ``retention_stub_seqs``: the projection renders, and declares
    # what is missing rather than hiding it.
    missing_result_stub_seqs: tuple[int, ...] = ()
    # The presence line frozen when the checkpoint this tail follows was
    # installed; rendered only where the mid-Turn resume anchor is.
    resume_presence_line: str | None = None

    @property
    def messages(self) -> tuple[PortableMessage, ...]:
        return tuple(message for turn in self.turns for message in turn.messages)


@dataclass
class _SourceTurn:
    turn_id: str | None
    status: str
    legacy_reason: str | None
    rows: list[dict[str, Any]]
    last_semantic_source_index: int
    boundary: HistoryBoundary | None = None
