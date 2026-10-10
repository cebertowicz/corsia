"""Result datatypes projected by the canonical Turn/input transition owner."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from infra.idle_checkpoints import IdleCheckpointDue
from infra.input_admission import AcceptedInput
from infra.thread_input_types import TurnAdmission


class TerminalResult(str, Enum):
    """Outcome of the exact Turn terminal compare-and-set."""

    APPLIED = "applied"
    ALREADY_SAME_OUTCOME = "already_same_outcome"
    CONFLICTING_TERMINAL = "conflicting_terminal"


@dataclass(frozen=True, slots=True)
class TerminalSettlement:
    """Durable effects committed by one terminal winner."""

    result: TerminalResult
    thread_id: str | None
    successor: TurnAdmission | None = None
    wake_thread_ids: tuple[str, ...] = ()
    switch_transition: object | None = None
    switch_request: object | None = None
    plan_dispatches_unknown: int = 0
    idle_checkpoint: IdleCheckpointDue | None = None
    failed_message_ids: tuple[str, ...] = ()
    probe_outcome: str | None = None


@dataclass(frozen=True, slots=True)
class StartupRecovery:
    """Actions made runnable by the one startup recovery transaction."""

    settlements: tuple[TerminalSettlement, ...]
    batches: tuple[TurnAdmission, ...]


@dataclass(frozen=True, slots=True)
class InputAdmission:
    """One accepted input and the Turn it atomically started, if idle."""

    accepted: AcceptedInput
    batch: TurnAdmission | None
    held_for_restart: bool = False
    passive: AcceptedInput | None = None
