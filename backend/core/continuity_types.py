"""Value contracts for addressable continuity entries."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


MAX_TEXT_CHARS = 4_096
MAX_LIST_ITEMS = 64
MAX_SOURCE_REFS = 16

BoundedText = Annotated[str, Field(min_length=1, max_length=MAX_TEXT_CHARS)]
SourceKind = Literal[
    "message",
    "turn",
    "file",
    "artifact",
    "runtime",
    "repository",
    "user",
    "other",
]


class ContinuityValue(BaseModel):
    """A strict immutable continuity value supplied through the edit tool."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _SourceReference(ContinuityValue):
    kind: SourceKind
    reference: BoundedText


SourceRefs = Annotated[list[_SourceReference], Field(max_length=MAX_SOURCE_REFS)]


class _Statement(ContinuityValue):
    statement: BoundedText
    source_refs: SourceRefs


class _Decision(ContinuityValue):
    statement: BoundedText
    rationale: BoundedText
    source_refs: SourceRefs


class _RelevantState(ContinuityValue):
    kind: Literal["file", "artifact", "runtime", "repository", "other"]
    reference: BoundedText
    state: BoundedText
    source_refs: SourceRefs


class _Outcome(ContinuityValue):
    result: BoundedText
    evidence: BoundedText
    source_refs: SourceRefs


Decision = _Decision
RelevantState = _RelevantState
Outcome = _Outcome


ENTRY_FIELD_MODELS = {
    "active_constraints": _Statement,
    "approvals_and_preferences": _Statement,
    "decisions": _Decision,
    "rejected_alternatives": _Decision,
    "relevant_state": _RelevantState,
    "verification": _Outcome,
    "known_failures": _Outcome,
    "open_questions": _Statement,
    "blockers": _Statement,
    "next_actions": _Statement,
    "recently_completed": _Statement,
    "facts": _Statement,
    "hypotheses": _Statement,
}

ENTRY_FIELDS = tuple(ENTRY_FIELD_MODELS)
EVIDENCE_REQUIRED_FIELDS = frozenset(
    {"active_constraints", "approvals_and_preferences", "decisions"}
)
