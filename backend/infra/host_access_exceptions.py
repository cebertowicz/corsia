"""Normalization and matching for immutable host-access policy exceptions."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from host_access.redaction import CommandRedactionError, redact_shell_command
from host_access.refusal_messages import (
    LITERAL_CREDENTIAL_REFUSAL,
    normalization_refusal,
)


EXTERNAL_AGENT_POLICY = "external_agent"


def _text(
    value: Any,
    field: str,
    *,
    max_chars: int,
    validation_error: type[Exception],
) -> str:
    if not isinstance(value, str):
        raise validation_error(f"{field} must be a string")
    normalized = value.strip()
    if not normalized:
        raise validation_error(f"{field} must not be blank")
    if len(normalized) > max_chars:
        raise validation_error(f"{field} must not exceed {max_chars} characters")
    return normalized


def _external_exception(
    entry: Mapping[str, Any],
    *,
    index: int,
    max_reason_chars: int,
    max_command_chars: int,
    validation_error: type[Exception],
) -> dict[str, str]:
    required = {"policy", "reason", "command", "working_directory"}
    if set(entry) != required:
        raise validation_error(
            "external requested policy exception must contain exactly policy, "
            "reason, command, and working_directory (and forbids repo_name)"
        )
    command = entry["command"]
    if not isinstance(command, str) or not command.strip():
        raise validation_error(
            f"requested_policy_exceptions[{index}].command must be a nonempty string"
        )
    if len(command) > max_command_chars:
        raise validation_error(
            f"requested_policy_exceptions[{index}].command must not exceed "
            f"{max_command_chars} characters"
        )
    try:
        redacted = redact_shell_command(command)
    except CommandRedactionError as exc:
        raise validation_error(normalization_refusal(exc)) from exc
    if redacted.literal_credentials:
        raise validation_error(LITERAL_CREDENTIAL_REFUSAL)
    raw_directory = entry["working_directory"]
    if not isinstance(raw_directory, str) or not raw_directory:
        raise validation_error(
            f"requested_policy_exceptions[{index}].working_directory must be an existing absolute directory"
        )
    directory = Path(raw_directory)
    if not directory.is_absolute() or not directory.is_dir():
        raise validation_error(
            f"requested_policy_exceptions[{index}].working_directory must be an existing absolute directory"
        )
    return {
        "policy": EXTERNAL_AGENT_POLICY,
        "reason": _text(
            entry["reason"],
            f"requested_policy_exceptions[{index}].reason",
            max_chars=max_reason_chars,
            validation_error=validation_error,
        ),
        "command": command,
        "working_directory": str(directory.resolve(strict=True)),
    }


def normalize_requested_policy_exceptions(
    value: Any,
    *,
    max_exceptions: int,
    max_repo_name_chars: int,
    max_reason_chars: int,
    max_command_chars: int,
    repo_policy_kinds: tuple[str, ...],
    validation_error: type[Exception],
) -> list[dict[str, str]]:
    """Normalize the repository and external exception union strictly."""
    exceptions = [] if value is None else value
    if not isinstance(exceptions, list):
        raise validation_error("requested_policy_exceptions must be an array")
    if len(exceptions) > max_exceptions:
        raise validation_error(
            f"requested_policy_exceptions must not exceed {max_exceptions} entries"
        )
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, ...]] = set()
    repo_keys = {"repo_name", "policy", "reason"}
    for index, entry in enumerate(exceptions):
        if not isinstance(entry, dict):
            raise validation_error("each requested policy exception must be an object")
        if entry.get("policy") == EXTERNAL_AGENT_POLICY:
            exception = _external_exception(
                entry,
                index=index,
                max_reason_chars=max_reason_chars,
                max_command_chars=max_command_chars,
                validation_error=validation_error,
            )
            identity = (
                EXTERNAL_AGENT_POLICY,
                exception["command"],
                exception["working_directory"],
            )
            if identity in seen:
                raise validation_error(
                    "requested_policy_exceptions contains a duplicate external launch"
                )
        else:
            if set(entry) != repo_keys:
                raise validation_error(
                    "each repository requested policy exception must contain exactly "
                    "repo_name, policy, and reason"
                )
            repo_name = _text(
                entry["repo_name"],
                f"requested_policy_exceptions[{index}].repo_name",
                max_chars=max_repo_name_chars,
                validation_error=validation_error,
            )
            policy = entry["policy"]
            if policy not in repo_policy_kinds:
                raise validation_error(
                    f"requested_policy_exceptions[{index}].policy is invalid"
                )
            exception = {
                "repo_name": repo_name,
                "policy": policy,
                "reason": _text(
                    entry["reason"],
                    f"requested_policy_exceptions[{index}].reason",
                    max_chars=max_reason_chars,
                    validation_error=validation_error,
                ),
            }
            identity = (repo_name, policy)
            if identity in seen:
                raise validation_error(
                    "requested_policy_exceptions contains a duplicate repo policy"
                )
        seen.add(identity)
        normalized.append(exception)
    return normalized


def external_agent_exception_matches(
    lease: Mapping[str, Any],
    *,
    command: str,
    working_directory: Path,
) -> bool:
    """Return whether immutable lease data binds this exact external launch."""
    exceptions = lease.get("requested_policy_exceptions")
    return isinstance(exceptions, list) and any(
        isinstance(exception, Mapping)
        and exception.get("policy") == EXTERNAL_AGENT_POLICY
        and exception.get("command") == command
        and exception.get("working_directory") == str(working_directory)
        for exception in exceptions
    )
