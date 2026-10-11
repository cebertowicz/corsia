"""Durable host-access verdict models and their schema versions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from host_access.failure_reasons import ApplicationReasonCategory
from llm.resolution import Harness

ApplicationSchemaVersion = Literal[3]
ActionSchemaVersion = Literal[1]
APPLICATION_REVIEW_SCHEMA_VERSION: Final[ApplicationSchemaVersion] = 3
ACTION_REVIEW_SCHEMA_VERSION: Final[ActionSchemaVersion] = 1


@dataclass(frozen=True, slots=True)
class ReviewerIdentity:
    """Exact trusted catalog identity, never supplied by model output."""

    catalog_id: str
    harness: Harness
    transport_model: str
    reported_model: str
    display_name: str


@dataclass(frozen=True, slots=True)
class ApplicationReview:
    """Validated application verdict plus the exact reviewer identity."""

    identity: ReviewerIdentity
    schema_version: ApplicationSchemaVersion
    verdict: Literal["allow", "reject"]
    reason_category: ApplicationReasonCategory
    reason: str
    user_attestation: str
    #: The requester's own model reviewed because the chosen reviewer could not run.
    reviewer_fallback: bool = False

    @property
    def allowed(self) -> bool:
        return self.verdict == "allow"


@dataclass(frozen=True, slots=True)
class ActionReview:
    """Validated single-action verdict plus the exact reviewer identity."""

    identity: ReviewerIdentity
    schema_version: ActionSchemaVersion
    verdict: Literal["allow", "block", "revoke"]
    reason: str

    @property
    def allowed(self) -> bool:
        return self.verdict == "allow"
