"""Schema fragment for continuity_edit's immutable correction-learning facts."""

from __future__ import annotations

from core.correction_learning import CAUSES, NEW_WORK_SIGNALS, TERMINAL_OUTCOMES, TIERS

LEARNING_OPERATIONS = ("triage", "outcome")
LEARNING_OPERATION_FIELDS = {
    "triage": {"subject_key", "tier", "cause", "new_work", "new_work_signal", "target_file", "serious_lowered_reason"},
    "outcome": {"subject_key", "outcome", "reason", "lesson_id", "workflow_version"},
}
LEARNING_REQUIRED_FIELDS = {
    "triage": ("subject_key", "tier"),
    "outcome": ("subject_key", "outcome"),
}
LEARNING_OPERATION_DESCRIPTION = (
    "triage records one immutable correction triage fact; outcome records its one terminal outcome. "
)
LEARNING_FIELD_SCHEMAS: dict[str, dict[str, object]] = {
    "subject_key": {
        "type": "string", "minLength": 1,
        "description": "The immutable correction subject named by the correction note.",
    },
    "tier": {
        "type": "string", "enum": list(TIERS),
        "description": "The System correction tier.",
    },
    "cause": {
        "type": "string", "enum": list(CAUSES),
        "description": "The first matching System correction cause. Required for a miss or serious; omit for not_correction.",
    },
    "new_work": {
        "type": "string", "enum": ["yes", "no"],
        "description": "Required for every tier but not_correction, which records only its tier.",
    },
    "new_work_signal": {
        "type": "string", "enum": list(NEW_WORK_SIGNALS),
        "description": "Required when new_work is yes; omitted otherwise.",
    },
    "target_file": {
        "type": "string", "minLength": 1,
        "description": "A file loaded in this turn that the correction changes. Omit when none was loaded or changed.",
    },
    "serious_lowered_reason": {
        "type": "string", "minLength": 1,
        "description": "Why an automatically proposed Serious tier was lowered, if applicable.",
    },
    "outcome": {"type": "string", "enum": list(TERMINAL_OUTCOMES)},
    "lesson_id": {"type": "string", "minLength": 1},
    "workflow_version": {"type": "string", "minLength": 1},
}
