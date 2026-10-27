"""Assemble the model-visible text of a finished foreground Bash command."""

from __future__ import annotations

from host_access.processes import ForegroundProcessResult
from events.connectors import ObservedArtifactDecl
from tools.unix_socket_policy import thread_bind_root_withheld_reason

REGISTRATION_WARNING_RESULT_CAP = 5_000


def append_denial_guidance(
    output_parts: list[str], thread_id: str, *guidance: str | None,
) -> None:
    """Add policy guidance, then any withheld bind-root reason, to a sandbox denial."""
    output_parts.extend(f"\n{line}" for line in guidance if line)
    if reason := thread_bind_root_withheld_reason(thread_id):
        output_parts.append(
            "\nAF_UNIX bind root not granted for this shell: "
            f"{reason}. Sockets can still be bound under the workspace."
        )


def registration_warning(
    artifact: ObservedArtifactDecl | None,
    error: Exception,
) -> str:
    """Return actionable repair guidance for failed artifact observation."""
    error_summary = " ".join(str(error).splitlines())[:300]
    if not error_summary:
        error_summary = type(error).__name__
    if artifact is None:
        repair = (
            "Run artifact_register with the connector identity shown in the "
            "successful command output."
        )
    else:
        repair = (
            "Run artifact_register with "
            f"integration='{artifact.integration}', type='{artifact.type}', and "
            f"external_id='{artifact.external_id}'."
        )
    return (
        "Warning: CORSIA couldn't register the file the command's output names "
        f"to this chat: {error_summary}. {repair}"
    )


def with_registration_warning(result: str, warning: str) -> str:
    """Reserve Bash's real result budget for an actionable warning suffix."""
    suffix = f"\n\n{warning}"
    if len(result) + len(suffix) <= REGISTRATION_WARNING_RESULT_CAP:
        return result + suffix
    marker = "\n... (output truncated to preserve registration warning)"
    prefix_len = max(0, REGISTRATION_WARNING_RESULT_CAP - len(marker) - len(suffix))
    return result[:prefix_len] + marker + suffix


def with_shell_warnings(
    result: str,
    registration_warning: str | None,
    ownership_notices: list[str],
    notes: list[str] | None = None,
) -> str:
    """Append all post-command warnings while preserving their output budget, then any notes.

    Notes (observe-rule outcomes) are information, not warnings: they keep the normal budget."""
    warnings = [
        *([registration_warning] if registration_warning else []),
        *ownership_notices,
    ]
    if warnings:
        result = with_registration_warning(result, "\n\n".join(warnings))
    elif len(result) > 10_000:
        truncated_chars = len(result) - 10_000
        result = result[:10_000] + f"\n... (truncated, {truncated_chars} more chars)"
    return "\n\n".join([result, *notes]) if notes else result


def foreground_output_parts(
    owned: ForegroundProcessResult,
) -> tuple[list[str], str, str]:
    """Return (output parts, decoded stdout, decoded stderr) for a result."""
    output_parts: list[str] = []
    stdout_text = ""
    stderr_text = ""

    if owned.stdout:
        stdout_text = owned.stdout.decode("utf-8", errors="replace")
        output_parts.append(stdout_text)

    if owned.stderr:
        stderr_text = owned.stderr.decode("utf-8", errors="replace")
        if stderr_text.strip():
            output_parts.append(f"STDERR:\n{stderr_text}")

    if owned.returncode != 0:
        output_parts.append(f"\nExit code: {owned.returncode}")
    if owned.undeclared_background_stopped:
        output_parts.append(
            "\nWarning: undeclared background work remained after the "
            "foreground command exited and was stopped."
        )
    return output_parts, stdout_text, stderr_text
