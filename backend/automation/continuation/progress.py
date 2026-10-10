"""Monotone progress classification for bounded plan continuation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    # plans.lifecycle imports this module, so plans stays out of import time.
    from plans.public import PlanLivenessSignal

ADVANCED = "advanced"
CHURNED = "churned"
UNCHANGED = "unchanged"


class ProgressState(Protocol):
    """Stored fields required to compare a liveness signal."""

    actionable_count: int
    signal_hash: str
    settled_events: int


@dataclass(frozen=True)
class ProgressSnapshot:
    """Immutable stored liveness snapshot."""

    actionable_count: int
    signal_hash: str
    settled_events: int


def classify_progress(
    current: PlanLivenessSignal,
    snapshot: ProgressState,
) -> str:
    """Classify forward progress separately from non-terminal churn."""

    if (
        current.count < snapshot.actionable_count
        or current.settled_events > snapshot.settled_events
    ):
        return ADVANCED
    if current.signal_hash != snapshot.signal_hash:
        return CHURNED
    return UNCHANGED


def wake_snapshot(wake) -> ProgressSnapshot:
    """Project a persisted wake into the comparison contract."""

    return ProgressSnapshot(
        actionable_count=wake.last_open_count,
        signal_hash=wake.last_signal_hash,
        settled_events=wake.last_settled_events,
    )
