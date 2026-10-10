"""Value types and channel identifiers for reviewer publication."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from reviewer.reviewer_categories import SEVERITY_BLOCK

CHANNEL_LINE = "line"
CHANNEL_FILE = "file"
CHANNEL_PR_LEVEL = "pr-level"
CHANNEL_THREAD = "thread"
CHANNEL_NONE = "none"
REASON_NO_ANCHOR = "no file anchor"
REASON_ANCHOR_REJECTED = "GitHub rejected the anchored comment"


class ReviewPublicationError(RuntimeError):
    """A review could not be published in full — infra failure, not a verdict.

    ``lost_block`` marks the hard-fail case: a ``block``-severity finding the
    reviewer made never reached the PR at all.
    """

    def __init__(self, message: str, *, lost_block: bool) -> None:
        super().__init__(message)
        self.lost_block = lost_block


@dataclass(frozen=True)
class FindingDelivery:
    """Where exactly one finding landed, and why its channel degraded."""

    finding: dict[str, Any]
    channel: str
    reason: str = ""

    @property
    def delivered(self) -> bool:
        """True when the finding reached a comment a reader can actually see."""
        return self.channel != CHANNEL_NONE

    @property
    def is_block(self) -> bool:
        """True for a ``block``-severity finding."""
        return str(self.finding.get("severity") or "").lower() == SEVERITY_BLOCK

    def label(self) -> str:
        """Identify the finding by severity, category, and location."""
        severity = str(self.finding.get("severity") or "unknown").upper()
        category = self.finding.get("category") or "uncategorized"
        return f"{severity} [{category}] {self._location()}"

    def _location(self) -> str:
        """Render the finding's file/line, or an explicit no-anchor marker."""
        path = str(self.finding.get("file") or "").strip()
        if not path:
            return "(no file)"
        line = self.finding.get("line")
        if isinstance(line, int) and not isinstance(line, bool) and line > 0:
            return f"{path}:{line}"
        return path


@dataclass(frozen=True)
class ReviewPublication:
    """The delivery record for every finding one review run tried to publish."""

    deliveries: list[FindingDelivery]

    @property
    def undelivered(self) -> list[FindingDelivery]:
        """Findings that reached no channel at all — the lost ones."""
        return [delivery for delivery in self.deliveries if not delivery.delivered]

    @property
    def pr_level(self) -> list[FindingDelivery]:
        """Findings delivered as PR-level comments because anchoring failed."""
        return [delivery for delivery in self.deliveries if delivery.channel == CHANNEL_PR_LEVEL]

    @property
    def lost_blocks(self) -> list[FindingDelivery]:
        """Undelivered ``block`` findings — each one hard-fails the review."""
        return [delivery for delivery in self.undelivered if delivery.is_block]
