"""Field-level JSON Schema fragments shared by the structured plan tools.

One parameter's type, bounds and agent-facing description are declared once
here; `plan_schemas` composes them into the per-operation contracts.
"""

from __future__ import annotations

from typing import Any

from core.size_rating import SIZE_CLASSES, SIZE_SCALE_DESCRIPTION, SIZE_TOOL_PREFIX
from plans import public
from prompts.plan_tools import NEXT_NODE_ID_DESCRIPTION
from registry.attention import DEFER_CONDITION_TYPES

_NODE_REVISION = {
    "type": "integer",
    "minimum": 0,
    "description": (
        "The target node's revision from plan_read, not the plan revision."
    ),
}
_DECISION_REVISION = {
    "type": "integer",
    "minimum": 0,
    "description": (
        "The decision revision from its projection, not a node or plan revision."
    ),
}
TEXT = {"type": "string", "minLength": 1}
NULLABLE_TEXT = {"type": ["string", "null"]}

MUTATION_FIELD_SCHEMAS: dict[str, dict[str, Any]] = {
    "kind": {
        "type": "string",
        "enum": sorted(public.NODE_KINDS),
        "description": "The plan node kind.",
    },
    # The plan pane already renders state and counts. A title only names the
    # outcome; repeating live status here makes it stale and harder to scan.
    "title": {
        **TEXT,
        "maxLength": public.MAX_NODE_TITLE,
        "description": (
            "Name the outcome in 1-3 words; no status or counts."
        ),
    },
    "acceptance_criteria": {
        **TEXT,
        "description": "Observable evidence required before this node is done.",
    },
    "parent_id": {
        **NULLABLE_TEXT,
        "description": "Optional parent node id in this plan.",
    },
    "description": {
        **NULLABLE_TEXT,
        "description": "Optional supporting detail.",
    },
    # Runtime validation owns the 200-character bound; repeating it in each
    # operation schema spends the published contract budget without teaching.
    "workflow_ref": {"type": "string"},
    "workflow_step": {"type": "string"},
    # `workflow_fields` owns the rubric and rejects invalid levels with its
    # agent-facing refusal; keep the published contract to the stored shape.
    "difficulty": {"type": ["string", "null"]},
    # The one agent-facing rubric: validation errors and the task-discipline skill
    # quote the same SIZE_SCALE_DESCRIPTION, and a test pins all three together.
    "size": {
        "type": ["string", "null"],
        "enum": [*SIZE_CLASSES, None],
        "description": (
            f"{SIZE_TOOL_PREFIX}{SIZE_SCALE_DESCRIPTION}. Set it on every task you add."
        ),
    },
    "difficulty_rerated": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "level": {"type": "string"},
            "reason": TEXT,
        },
        "required": ["level", "reason"],
    },
    "checkpoint_exception": {
        "type": "boolean",
        "description": "True only for an explicitly approved checkpoint exception.",
    },
    "assignee": {
        "type": ["string", "null"],
        "enum": [*sorted(public.ASSIGNEES), None],
        "description": "Optional human or agent assignment class.",
    },
    "spawn_kind": {
        **NULLABLE_TEXT,
        "description": (
            "Optional delegated-worker relationship kind."
        ),
    },
    "reason": {
        **TEXT,
        "description": (
            "Why work cannot advance or will not be done; on arm_watch, "
            "schedules the task until the deadline (never with artifact_id)."
        ),
    },
    "reason_key": {
        **TEXT,
        "description": "Stable machine-readable cause, such as missing_instructions.",
    },
    "owner_kind": {
        "type": "string",
        "enum": ["external", "system", "thread", "parent", "user"],
        "description": "Who ends the blocker; parent is your delegating agent; user requires ask_artifact_id.",
    },
    "owner_id": {
        **NULLABLE_TEXT,
        "description": "Optional exact identity of the accountable party.",
    },
    "ask_artifact_id": {
        **TEXT,
        "description": "Owned document or image to review, visuals embedded inline; never a decision.",
    },
    "hold_id": {**TEXT, "description": "The exact pause or blocker to clear."},
    "watchdog_budget_seconds": {
        "type": ["integer", "null"],
        "minimum": 1,
        "description": "Optional positive liveness watchdog budget in seconds.",
    },
    "before_node_id": {
        **TEXT,
        "description": "Optional sibling node before which this node is inserted.",
    },
    "node_id": {**TEXT, "description": "The target node id in this plan."},
    "node_ids": {
        "type": "array",
        "minItems": 1,
        "maxItems": public.MAX_ARCHIVE_NODES,
        "uniqueItems": True,
        "items": TEXT,
        "description": (
            "archive_node: several node ids at once."
        ),
    },
    "expected_revision": _NODE_REVISION,
    "order_key": {
        "type": "integer",
        "minimum": 0,
        "description": "The target zero-based sibling order.",
    },
    "feedback": {**TEXT, "description": "Actionable rejection feedback."},
    "evidence": {
        **TEXT,
        "description": (
            "Concrete evidence against the current requirements."
        ),
    },
    "artifact_ids": {
        "type": "array",
        "uniqueItems": True,
        "items": TEXT,
        "description": (
            "Result artifacts this thread owns that support the evidence."
        ),
    },
    "affected_nodes": {
        "type": "array",
        "minItems": 1,
        "items": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "node_id": TEXT,
                "expected_revision": _NODE_REVISION,
            },
            "required": ["node_id", "expected_revision"],
        },
        "description": "Unfinished nodes that must wait for this correction.",
    },
    "spawned_thread_id": {
        **TEXT,
        "description": (
            "The exact delegated child thread to attach, or (replace) to hand "
            "the node to."
        ),
    },
    "verification_result": {
        "type": "string",
        "enum": ["passed"],
        "description": "Acceptance requires the literal value 'passed'.",
    },
    "condition": {
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "enum": sorted(DEFER_CONDITION_TYPES),
                "description": "The condition category.",
            },
            # Validity is owned by registry.attention.validate_defer_condition,
            # which every caller shares; this describes the value rather than
            # re-deciding it, so the two cannot drift apart.
            "value": {
                "type": "string",
                "description": (
                    "ISO-8601 time with zone for 'deadline'; otherwise the "
                    "event, dependency or milestone name."
                ),
            },
        },
        "required": ["type", "value"],
        "description": (
            "What wakes this plan (see arm_watch example). Deadline: when "
            "you'd act, not a far backstop."
        ),
    },
    "artifact_id": {
        **TEXT,
        "description": (
            "Artifact id (not external id) of your artifact, e.g. a PR, whose "
            "event ends this wait."
        ),
    },
    "expected_event": {
        **TEXT,
        "description": (
            "The event that ends this wait; required with artifact_id."
        ),
    },
    "watch_id": {**TEXT, "description": "The armed watch to resolve."},
    "depends_on_node_id": {
        **TEXT,
        "description": "The node that must finish first, in any plan you can read.",
    },
    "next_node_id": {**TEXT, "description": NEXT_NODE_ID_DESCRIPTION},
}

# Per-operation mutation semantics are encoded by the exact field contracts below.

_OPTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "id": {"type": "string", "enum": list(public.DECISION_OPTION_IDS)},
        "label": {
            "type": "string",
            "minLength": 1,
            "maxLength": public.DECISION_MAX_OPTION_LABEL,
        },
        "detail": {
            "type": "string",
            "maxLength": public.DECISION_MAX_OPTION_DETAIL,
        },
    },
    "required": ["id", "label"],
}
DECISION_FIELD_SCHEMAS: dict[str, dict[str, Any]] = {
    "decision_id": {**TEXT, "description": "The target plan decision id."},
    "source_node_id": {
        **TEXT,
        "description": "The plan node whose progress requires this decision.",
    },
    "expected_revision": _DECISION_REVISION,
    "title": {
        "type": "string",
        "minLength": 1,
        "maxLength": public.DECISION_MAX_TITLE,
    },
    "context": {
        "type": "string",
        "minLength": 1,
        "maxLength": public.DECISION_MAX_CONTEXT,
    },
    "tradeoff": {
        "type": "string",
        "minLength": 1,
        "maxLength": public.DECISION_MAX_TRADEOFF,
    },
    "pick": {
        "type": "string",
        "enum": [public.PICK_ONE, public.PICK_ANY],
        "description": (
            '"one" (default): choose exactly one. "any": independent items, tick '
            'any (or none). Use "any" instead of options like "All of these" or '
            '"All except C". A tradeoff gets its own "one" decision.'
        ),
    },
    "recommendation": {
        "type": ["string", "array"],
        "items": {"type": "string", "enum": list(public.DECISION_OPTION_IDS)},
        "description": 'One option id; for pick "any", the list to pre-tick, e.g. ["A","C"], or [].',
    },
    "rationale": {
        "type": "string",
        "minLength": 1,
        "maxLength": public.DECISION_MAX_RATIONALE,
    },
    "options": {
        "type": "array",
        "minItems": public.DECISION_MIN_OPTIONS,
        "maxItems": public.DECISION_MAX_OPTIONS,
        "items": _OPTION_SCHEMA,
    },
    "other_enabled": {
        "type": "boolean",
        "const": True,
        "description": "Must be true so the user is never stranded.",
    },
    "order_key": {
        "type": "integer",
        "minimum": 0,
        "description": "The target decision order.",
    },
}
# Decision operation semantics share the same exact conditional-schema shape.
