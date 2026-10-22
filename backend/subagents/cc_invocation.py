"""One cache-stable Claude Code process invocation description."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Sequence
from dataclasses import asdict, dataclass

from claude_controls import CONFINED_PERMISSION_MODE, confined_claude_controls
from subagents.managed_launch import claude_project_discovery_args

#: A launch with every CORSIA tool, as every managed child had before confinement.
CONFINEMENT_NONE = "none"
#: A launch that may only Read, Grep and Glob its working directory and add-dirs.
CONFINEMENT_REVIEW = "review"
CONFINEMENTS = (CONFINEMENT_NONE, CONFINEMENT_REVIEW)
_CONFINEMENT_FIELDS = ("confinement", "add_dirs")
#: The only tools a review launch has: Claude Code's own, run inside the provider.
REVIEW_NATIVE_TOOLS = ("Read", "Grep", "Glob")


def claude_review_profile_args() -> list[str]:
    """Return the fixed Claude Code tool profile for a confined review child.

    The caller supplies the reviewed paths with ``--add-dir``. Keeping this
    profile separate from normal managed-session construction prevents a review
    child from inheriting CORSIA MCP access, local settings, or any tool other
    than Read, Grep, and Glob.
    """
    return [
        *confined_claude_controls(),
        "--tools", ",".join(REVIEW_NATIVE_TOOLS),
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
    confinement: str = CONFINEMENT_NONE
    add_dirs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_confinement(self.confinement, self.add_dirs)
        if self.confinement == CONFINEMENT_REVIEW and self.mcp_config:
            raise ValueError("a confined Claude Code launch has no CORSIA tool bridge")


def _require_confinement(confinement: object, add_dirs: object) -> None:
    if confinement not in CONFINEMENTS:
        raise ValueError(f"unknown Claude Code launch confinement: {confinement!r}")
    if not isinstance(add_dirs, tuple) or any(
        not isinstance(path, str) or not path for path in add_dirs
    ):
        raise ValueError("Claude Code add-dirs must be non-empty path strings")
    if (confinement == CONFINEMENT_REVIEW) != bool(add_dirs):
        raise ValueError("only a review launch has add-dirs, and it needs at least one")


@dataclass(frozen=True)
class LaunchConfinement:
    """What one launch of a thread may reach, read once at the launch's start."""

    mode: str = CONFINEMENT_NONE
    add_dirs: tuple[str, ...] = ()
    working_dir: str | None = None  # a review child's own directory

    @property
    def confined(self) -> bool:
        return self.mode != CONFINEMENT_NONE


UNCONFINED = LaunchConfinement()


def permission_mode_for(confinement: str) -> str:
    """The permission mode a launch runs with; review's is its profile's own."""
    return CONFINED_PERMISSION_MODE if confinement == CONFINEMENT_REVIEW else "bypassPermissions"


def stored_confinement(value: dict) -> tuple[str, tuple[str, ...]]:
    """Read one stored launch's confinement; a record without one is unconfined.

    Raises ``ValueError`` for a malformed value, so no reader can mistake a
    damaged review record for an unconfined one.
    """
    confinement = value.get("confinement", CONFINEMENT_NONE)
    add_dirs = value.get("add_dirs", [])
    if not isinstance(add_dirs, list):
        raise ValueError("Claude Code add-dirs must be a list")
    _require_confinement(confinement, tuple(add_dirs))
    return str(confinement), tuple(add_dirs)


def claude_invocation_metadata(invocation: ClaudeInvocation) -> dict[str, object]:
    """Serialize one launch description for the durable CC session record.

    An unconfined launch stores exactly the fields it stored before
    confinement existed.
    """
    value: dict[str, object] = asdict(invocation)
    if invocation.confinement == CONFINEMENT_NONE:
        for field in _CONFINEMENT_FIELDS:
            value.pop(field)
    else:
        value["add_dirs"] = list(invocation.add_dirs)
    return value


def claude_invocation_from_metadata(value: object) -> ClaudeInvocation | None:
    """Restore one exact launch description only from a complete valid record."""
    if not isinstance(value, dict):
        return None
    plain = {key: item for key, item in value.items() if key not in _CONFINEMENT_FIELDS}
    fields = set(ClaudeInvocation.__dataclass_fields__) - set(_CONFINEMENT_FIELDS)
    if set(plain) != fields or any(
        item is not None and not isinstance(item, str) for item in plain.values()
    ):
        return None
    binary = plain["binary"]
    permission_mode = plain["permission_mode"]
    if not isinstance(binary, str) or not binary:
        return None
    if not isinstance(permission_mode, str) or not permission_mode:
        return None
    try:
        confinement, add_dirs = stored_confinement(value)
        return ClaudeInvocation(**plain, confinement=confinement, add_dirs=add_dirs)
    except ValueError:
        return None


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


def _review_args(invocation: ClaudeInvocation) -> list[str]:
    """Read-only tools over the add-dirs; the profile sets the permission mode."""
    args = [*claude_review_profile_args(), "--disable-slash-commands"]
    for path in invocation.add_dirs:
        args += ["--add-dir", path]
    return args


def _managed_args(invocation: ClaudeInvocation) -> list[str]:
    """Project settings plus the CORSIA tool bridge, or no tools without one."""
    args = claude_project_discovery_args()
    if invocation.mcp_config:
        from tools.catalog import MCP_CORSIA_PREFIX

        return [
            *args, "--tools", f"{MCP_CORSIA_PREFIX}*", "--disable-slash-commands",
            "--mcp-config", invocation.mcp_config, "--strict-mcp-config",
        ]
    return [
        *args, "--tools", "", "--disable-slash-commands",
        "--mcp-config", '{"mcpServers":{}}', "--strict-mcp-config",
    ]


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
    ]
    confined = invocation.confinement == CONFINEMENT_REVIEW
    if not confined:
        command += ["--permission-mode", invocation.permission_mode]
    if invocation.resume_session_id:
        command += ["--resume", invocation.resume_session_id]
    elif invocation.session_id:
        command += ["--session-id", invocation.session_id]
    if invocation.seed_file:
        command += ["--append-system-prompt-file", invocation.seed_file]
    command += _review_args(invocation) if confined else _managed_args(invocation)
    if invocation.model:
        command += ["--model", invocation.model]
    if invocation.effort:
        command += ["--effort", invocation.effort]
    return [*command, *extra_args]
