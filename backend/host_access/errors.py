"""Leaf exceptions shared by host-access supervision helpers."""

from __future__ import annotations

from dataclasses import dataclass


class HostAccessSupervisorError(RuntimeError):
    """The reviewed lease cannot safely authorize host execution."""


@dataclass(frozen=True, slots=True)
class ReviewerOutage:
    """One reviewer model that cannot run now, and the cause the runtime reported."""

    model_name: str
    reason: str
    resets_at: str | None = None


class ReviewerUnavailableError(HostAccessSupervisorError):
    """A reviewer model cannot run, and nothing else was allowed to review.

    ``fallback`` is the requester's own model when fallback was tried and also
    could not run. ``fallback_enabled`` and ``pinned`` let the refusal say which
    remedy applies: a pinned (per-action) review never falls back.
    """

    def __init__(
        self,
        message: str,
        *,
        outage: ReviewerOutage,
        pinned: bool = False,
        fallback_enabled: bool = False,
        fallback: ReviewerOutage | None = None,
    ) -> None:
        super().__init__(message)
        self.outage = outage
        self.pinned = pinned
        self.fallback_enabled = fallback_enabled
        self.fallback = fallback
