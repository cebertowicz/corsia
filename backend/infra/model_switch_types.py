"""Value types the model-switch boundary returns to its callers.

These describe a switch as the API, thread and delivery layers see it: what
was requested, and how it settled. They carry no database access, so the
surfaces that only need to read the shape of a switch do not import the
transactional boundary in ``infra.model_switches``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SwitchRequest:
    """An accepted switch request, not yet a committed model change."""

    operation_id: str
    thread_id: str
    source_model: str
    destination_model: str
    source_execution_epoch: int
    destination_execution_epoch: int
    switch_after_input_order: int
    actor_kind: str
    actor_id: str | None
    reason: str
    checkpoint_choice: str
    requested_at: str
    observed_input_order: int
    waiting_for_source: bool = False
    duplicate: bool = False
    updated_input_ids: tuple[str, ...] = ()

    @property
    def status(self) -> str:
        return "waiting_for_turn" if self.waiting_for_source else "preparing"


@dataclass(frozen=True, slots=True)
class SwitchTransition:
    """How a requested switch settled: committed to the destination, or failed."""

    thread_id: str
    operation_id: str
    committed: bool
    source_model: str | None = None
    destination_model: str | None = None
    # The cause the database actually holds, which is the first failure's, not
    # necessarily the one this caller passed.
    error: str | None = None

    @property
    def status(self) -> str:
        return "committed" if self.committed else "failed"
