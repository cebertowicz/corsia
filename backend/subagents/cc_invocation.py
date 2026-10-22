"""One cache-stable Claude Code process invocation description."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Sequence
from dataclasses import asdict, dataclass

from claude_controls import confined_claude_controls
from subagents.managed_launch import claude_project_discovery_args


def claude_review_profile_args() -> list[str]:
    """Return the fixed Claude Code tool profile for a confined review child.

    The caller supplies the reviewed paths with ``--add-dir``. Keeping this
    profile separate from normal managed-session construction prevents a review
    child from inheriting CORSIA MCP access, local settings, or any tool other
    than Read, Grep, and Glob.
    """
    return [
        *confined_claude_controls(),
        "--tools", "Read,Grep,Glob",
    ]


@dataclass(frozen=True, slots=True)
class ClaudeInvocation:
    """Everything that decides what a ``claude -p`` command line looks like."""

    binary: str
    permission_mode: str
    session_id: str | None = None
    resume_session_id: str | None = None
    seed_file: str | None = None
    mcp_config: str | None = None
    working_dir: str | None = None
    model: str | None = None
    effort: str | None = None


def claude_invocation_metadata(invocation: ClaudeInvocation) -> dict[str, str | None]:
    """Serialize one launch description for the durable CC session record."""
    return asdict(invocation)


def claude_invocation_from_metadata(value: object) -> ClaudeInvocation | None:
    """Restore one exact launch description only from a complete valid record."""
    if not isinstance(value, dict):
        return None
    fields = ClaudeInvocation.__dataclass_fields__
    if set(value) != set(fields) or any(
        item is not None and not isinstance(item, str) for item in value.values()
    ):
        return None
    binary = value["binary"]
    permission_mode = value["permission_mode"]
    if not isinstance(binary, str) or not binary:
        return None
    if not isinstance(permission_mode, str) or not permission_mode:
        return None
    return ClaudeInvocation(**value)


def claude_transcript_prefix_proof(
    path: str,
    prefix_bytes: int | None = None,
) -> tuple[int, str] | None:
    """Hash one exact transcript prefix, or return None if it is unavailable."""
    if prefix_bytes is not None and (
        isinstance(prefix_bytes, bool) or prefix_bytes < 0
    ):
        return None
    try:
        with open(path, "rb") as stream:
            file_bytes = os.fstat(stream.fileno()).st_size
            expected = file_bytes if prefix_bytes is None else prefix_bytes
            if file_bytes < expected:
                return None
            digest = hashlib.sha256()
            remaining = expected
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    return None
                digest.update(chunk)
                remaining -= len(chunk)
    except OSError:
        return None
    return expected, digest.hexdigest()


def build_claude_cmd(
    invocation: ClaudeInvocation,
    *,
    stream_input: bool,
    extra_args: Sequence[str] = (),
) -> list[str]:
    """Assemble live and checkpoint commands from the same cache-bearing prefix."""
    command = [invocation.binary, "-p"]
    if stream_input:
        command += ["--input-format", "stream-json", "--replay-user-messages"]
    command += [
        "--output-format", "stream-json", "--verbose",
        "--include-partial-messages", "--include-hook-events",
        "--permission-mode", invocation.permission_mode,
    ]
    if invocation.resume_session_id:
        command += ["--resume", invocation.resume_session_id]
    elif invocation.session_id:
        command += ["--session-id", invocation.session_id]
    if invocation.seed_file:
        command += ["--append-system-prompt-file", invocation.seed_file]
    command += claude_project_discovery_args()
    if invocation.mcp_config:
        from tools.catalog import MCP_CORSIA_PREFIX

        command += [
            "--tools", f"{MCP_CORSIA_PREFIX}*", "--disable-slash-commands",
            "--mcp-config", invocation.mcp_config, "--strict-mcp-config",
        ]
    else:
        command += [
            "--tools", "", "--disable-slash-commands",
            "--mcp-config", '{"mcpServers":{}}', "--strict-mcp-config",
        ]
    if invocation.model:
        command += ["--model", invocation.model]
    if invocation.effort:
        command += ["--effort", invocation.effort]
    return [*command, *extra_args]
