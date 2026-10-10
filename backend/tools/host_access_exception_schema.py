"""Public schema for immutable host-access policy exceptions."""

from __future__ import annotations

from infra.host_access_leases import (
    MAX_ACTION_DESCRIPTOR_CHARS,
    MAX_POLICY_EXCEPTION_REASON_CHARS,
    MAX_POLICY_EXCEPTION_REPO_NAME_CHARS,
)


def policy_exception_schema() -> dict[str, object]:
    """Return the strict repository-or-external exception union."""
    return {
        "oneOf": [
            {
                "type": "object",
                "required": ["repo_name", "policy", "reason"],
                "additionalProperties": False,
                "properties": {
                    "repo_name": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_POLICY_EXCEPTION_REPO_NAME_CHARS,
                    },
                    "policy": {
                        "type": "string",
                        "enum": [
                            "branch_pinning",
                            "protected_base_checkout",
                        ],
                    },
                    "reason": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_POLICY_EXCEPTION_REASON_CHARS,
                    },
                },
            },
            {
                "type": "object",
                "required": ["policy", "reason", "command", "working_directory"],
                "additionalProperties": False,
                "properties": {
                    "policy": {"const": "external_agent"},
                    "reason": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_POLICY_EXCEPTION_REASON_CHARS,
                    },
                    "command": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_ACTION_DESCRIPTOR_CHARS,
                    },
                    "working_directory": {"type": "string", "minLength": 1},
                },
            },
        ]
    }
