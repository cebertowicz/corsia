"""Operation-specific JSON Schemas for structured plan tools."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable

from plans import public
from tools.plan_operation_docs import PARAMS_DESCRIPTION, operation_description
from tools.plan_field_schemas import (
    DECISION_FIELD_SCHEMAS,
    MUTATION_FIELD_SCHEMAS,
    TEXT,
)

_UPDATE_OPERATION_PREAMBLE = (
    "activate and start both make the node your current task."
)


def published(contract: dict[str, Any]) -> dict[str, Any]:
    """Return the schema a model is shown: the contract minus its conditionals.

    The per-operation ``allOf`` rules stay the validator's contract. They are
    not published because the operation field's description already states
    each operation's required keys, and a refused call names the valid ones;
    Anthropic-API harnesses drop a top-level ``allOf`` anyway. Publishing
    them cost every agent about 9.5 KB per call (#1939).
    """
    return {key: value for key, value in contract.items() if key != "allOf"}


def _operation_rule(
    operations: list[str],
    fields: set[str],
    required: set[str],
    *,
    params_min_properties: int | None = None,
) -> dict[str, Any]:
    """Constrain operations with one identical parameter contract."""
    selector: dict[str, Any]
    if len(operations) == 1:
        selector = {"const": operations[0]}
    else:
        selector = {"enum": operations}
    params: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {field: {} for field in sorted(fields)},
        "required": sorted(required),
    }
    if params_min_properties is not None:
        params["minProperties"] = params_min_properties
    return {
        "if": {
            "type": "object",
            "properties": {"operation": selector},
        },
        "then": {
            "type": "object",
            "properties": {"params": params},
        },
    }


def _operation_schema(
    operations: list[str],
    operation_fields: dict[str, set[str]],
    required_fields: dict[str, set[str]],
    field_schemas: dict[str, dict[str, Any]],
    *,
    patch_operation: str,
    operation_preamble: str,
) -> dict[str, Any]:
    """Publish exact per-operation constraints without repeating field schemas."""
    all_fields = set().union(*(operation_fields[operation] for operation in operations))
    grouped: dict[
        tuple[tuple[str, ...], tuple[str, ...], int | None],
        list[str],
    ] = {}
    for operation in operations:
        min_properties = 3 if operation == patch_operation else None
        contract = (
            tuple(sorted(operation_fields[operation])),
            tuple(sorted(required_fields[operation])),
            min_properties,
        )
        grouped.setdefault(contract, []).append(operation)
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "plan_id": {
                **TEXT,
                "description": "The actor-bound plan id from plan_read or plan_ensure.",
            },
            "mutation_id": {
                **TEXT,
                "description": "Stable retry key for this one logical action.",
            },
            "operation": _operation_field(
                operations, required_fields, operation_preamble,
            ),
            "params": {
                "type": "object",
                "additionalProperties": False,
                "description": PARAMS_DESCRIPTION,
                "properties": {
                    field: deepcopy(field_schemas[field])
                    for field in sorted(all_fields)
                },
            },
        },
        "required": ["plan_id", "mutation_id", "operation", "params"],
        "allOf": [
            _operation_rule(
                grouped_operations,
                set(contract[0]),
                set(contract[1]),
                params_min_properties=contract[2],
            )
            for contract, grouped_operations in grouped.items()
        ],
    }


def _operation_field(
    operations: list[str], required_fields: dict[str, set[str]], preamble: str,
) -> dict[str, Any]:
    """Publish the operation enum with each operation's required keys."""
    return {
        "type": "string",
        "enum": operations,
        "description": operation_description(
            operations, required_fields, preamble=preamble,
        ),
    }


def update_schema(operations: Iterable[str]) -> dict[str, Any]:
    """Return the exhaustive discriminated contract for plan_update."""
    ordered = list(operations)
    agent_operations = set(public.OPERATION_FIELDS) - public.HUMAN_OPERATIONS
    if set(ordered) != agent_operations:
        raise RuntimeError("plan_update operation list and field contract diverged")
    required_fields = {
        operation: set(fields)
        for operation, fields in public.OPERATION_REQUIRED_FIELDS.items()
    }
    # The user's one-tap Mark done persists its own evidence; an agent must say
    # what it verified. The runtime repeats this check for non-tool callers.
    required_fields["mark_done"].add("evidence")
    return _operation_schema(
        ordered,
        public.OPERATION_FIELDS,
        required_fields,
        MUTATION_FIELD_SCHEMAS,
        patch_operation="edit",
        operation_preamble=_UPDATE_OPERATION_PREAMBLE,
    )


def decision_schema(operations: Iterable[str]) -> dict[str, Any]:
    """Return the exhaustive discriminated contract for plan_decision."""
    ordered = list(operations)
    if set(ordered) != set(public.DECISION_OPERATION_FIELDS):
        raise RuntimeError("plan_decision operation list and field contract diverged")
    return _operation_schema(
        ordered,
        public.DECISION_OPERATION_FIELDS,
        public.DECISION_OPERATION_REQUIRED_FIELDS,
        DECISION_FIELD_SCHEMAS,
        patch_operation="update",
        operation_preamble="",
    )


#: plan_read's and plan_ensure's parameters.
READ_PARAMS = {
    "mode": {
        "type": "string", "enum": ["status", "full"],
        "description": (
            "'status' (default): overview plus capped Current/Recent rows. "
            "'full': all nodes WITHOUT bodies, paged via continue_read; "
            "branches with no open work left are folded into per-level "
            "counts."
        ),
    },
    "node_id": {
        "type": "string",
        "description": (
            "One node in full: every field and recent events."
        ),
    },
    "section": {
        "type": "string", "enum": ["current", "recent"],
        "description": "The status section a cursor continues.",
    },
    "cursor": {
        "type": "string",
        "description": (
            "From continue_read."
        ),
    },
    "known_revision": {
        "type": "integer",
        "description": (
            "With mode='full' only: your plan.revision, answered with just "
            "unchanged: true when the plan is still at it."
        ),
    },
    "include_folded": {
        "type": "boolean",
        "description": (
            "With mode='full' only: true also lists folded nodes (finished, "
            "nothing open beneath them), each marked folded. A folded "
            "top-level branch leaves progress; node_id reads one without this."
        ),
    },
    # ``bounded`` is gone rather than deprecated in place: it asked for the
    # whole plan without bodies, which is exactly mode='full', and a second
    # published way in costs every request tokens. An old caller's key is
    # refused by name, with the valid keys listed, never silently ignored.
}

#: plan_read only: plan_ensure creates a plan, which only its owner may do.
THREAD_PARAM = {
    "thread_id": {
        "type": "string",
        "description": (
            "Read another thread's plan in your project instead of yours; "
            "read-only, and its reads come back addressed to it."
        ),
    },
}
