"""The ``bash`` tool's approved long-running background mode."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from host_access.managed_lifecycle import DEFAULT_STALL_AFTER_SECONDS

DESCRIPTION_LINE = (
    "`long_running` (with background): runs until exit, stop, or the 24-hour "
    "limit; you are sent an event when it goes silent or a terminal outcome can "
    "be verified. Max 2 per thread and 6 per CORSIA instance."
)

PROPERTIES: dict[str, Any] = {
    "long_running": {
        "type": "boolean",
        "description": (
            "With background: true, run until exit or the 24-hour limit. "
            "Requires description. Not available with host or timeout "
            "(default: false)."
        ),
    },
    "stall_after_seconds": {
        "type": "integer",
        "description": (
            "Only with long_running: seconds without output before a silence "
            f"event (default {DEFAULT_STALL_AFTER_SECONDS})."
        ),
        "minimum": 60,
        "maximum": 3600,
    },
}


@dataclass(frozen=True, slots=True)
class LongRunningCall:
    enabled: bool
    refusal: str | None
    spawn_options: dict[str, Any]
    action_params: dict[str, Any]

    @classmethod
    def from_shell_call(cls, call: Mapping[str, Any]) -> LongRunningCall:
        enabled = bool(call["long_running"])
        stalled = call["stall_after_seconds"]
        action = {
            key: call[key]
            for key in (
                "command", "description", "timeout", "working_dir", "background", "host",
            )
        }
        if enabled:
            action["long_running"] = True
        error = refusal(
            long_running=enabled,
            stall_after_seconds=stalled,
            background=bool(call["background"]),
            host=bool(call["host"]),
            timeout=call["timeout"],
            description=call["description"],
        )
        return cls(
            enabled,
            error,
            spawn_options(enabled, call["description"], stalled),
            action,
        )

    def started_message(self, handle: str) -> str:
        return started_message(handle, long_running=self.enabled)


def refusal(
    *,
    long_running: bool,
    stall_after_seconds: int | None,
    background: bool,
    host: bool,
    timeout: int | None,
    description: str | None,
) -> str | None:
    if not long_running:
        if stall_after_seconds is not None:
            return "Error: stall_after_seconds applies only with long_running: true."
        return None
    if not background:
        return "Error: long_running requires background: true."
    if host:
        return (
            "Error: long_running is not available with host: true; run it in "
            "protected scope."
        )
    if timeout is not None:
        return (
            "Error: timeout does not apply to a long-running process; omit "
            "timeout when using long_running."
        )
    if not (description or "").strip():
        return (
            "Error: long_running requires a description; it names the process "
            "in every event about it."
        )
    return None


def spawn_options(
    long_running: bool,
    description: str | None,
    stall_after_seconds: int | None,
) -> dict[str, Any]:
    if not long_running:
        return {}
    return {
        "long_running": True,
        "description": (description or "").strip(),
        "stall_after_seconds": (
            stall_after_seconds or DEFAULT_STALL_AFTER_SECONDS
        ),
    }


def started_message(handle: str, *, long_running: bool) -> str:
    message = (
        f"Managed background process started with handle {handle}. Use "
        "managed_process_status, managed_process_logs, managed_process_list, or "
        "managed_process_stop with that handle."
    )
    if long_running:
        message += (
            " It will run until it exits or reaches 24 hours. You will receive a "
            "managed_process.* event for a verifiable terminal outcome; do not poll."
        )
    return message
