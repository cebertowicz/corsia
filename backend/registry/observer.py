"""Artifact registration derived from persisted tool-call records."""

from __future__ import annotations

import json
import shlex

from loguru import logger

from doceditor.presence import is_document_activity_tool, observe_document_activity

from events.connectors import (
    CommandTriggerDecl,
    ObservedArtifactDecl,
    registered_command_triggers,
)
from infra.database import get_db
from registry.observation_result import (
    ObservationResult,
    observation_result,
    observe_settled_tool_result,
)
from registry.observed_write import register_observed_write
from registry.registration_writer import RegistrationOrigin
from registry.service import register_observed_artifact
from tools.catalog import CC_BUILTIN_ALIASES, normalize_tool_name
from tools.tool_surface import resolve_dispatch

# Canonical-name-keyed: harness-name variance is resolved once by
# ``normalize_tool_name`` before this table is ever consulted.
_WRITERS: dict[str, tuple[str, str | None]] = {
    "write_file": ("path", "description"),
    "edit_file": ("path", None),
    "multi_edit": ("path", None),
    "NotebookEdit": ("notebook_path", None),
}

_NATIVE_TOOLS = {"write_file", "edit_file", "multi_edit"}

_CANCELLED = "[Tool execution was cancelled]"

_CALL_SCAN_LIMIT = 30
_SHELL_PUNCTUATION = ";&|<>()"
_PARAMETER_START = frozenset("{([*@$#?!-$")


def observe_record(
    thread_id: str | None,
    role: str,
    content: str | None,
    metadata: dict | str | None,
    db=None,
    created_at: str | None = None,
    *,
    origin: RegistrationOrigin | None = None,
) -> ObservationResult | None:
    """Inspect one persisted message row for artifact and activity outcomes."""
    try:
        return _observe(thread_id, role, content, metadata, db, created_at, origin)
    except Exception as e:
        logger.warning(f"registry observer failed for session {thread_id}: {e}")
        return None


def _artifact_from_result(
    thread_id, raw_name, name, args, text, meta, arg_keys, command_triggers,
    *, is_cc_builtin, origin, db,
) -> dict | None:
    """Preserve existing registration behavior beside activity observation."""
    if arg_keys is None:
        return observe_command_result(
            thread_id,
            raw_name,
            args,
            text,
            is_error=bool(meta.get("is_error")),
            origin=origin,
            db=db,
            notify=False,
        ) if command_triggers else None
    if meta.get("is_error"):
        return None
    if name in _NATIVE_TOOLS and not is_cc_builtin and text.startswith("{"):
        try:
            receipt = json.loads(text)
        except (TypeError, ValueError):
            receipt = None
        if isinstance(receipt, dict) and receipt.get("registry_managed") is True:
            return None
    if name in _NATIVE_TOOLS and not is_cc_builtin and (
        text == _CANCELLED or text.startswith(("Error", "Warning"))
    ):
        return None
    return register_observed_write(
        thread_id, args, arg_keys, text,
        name=name, is_cc_builtin=is_cc_builtin, origin=origin, db=db,
    )


def _observe(
    thread_id, role, content, metadata, db, created_at, origin,
) -> ObservationResult | None:
    if not thread_id or role != "tool":
        return None
    meta = _as_dict(metadata)
    if meta.get("synthetic_tool_result") is True:
        return None
    tool_call_id = meta.get("tool_call_id")
    raw_name = meta.get("tool_name") or meta.get("name")
    if not tool_call_id:
        return None
    name = normalize_tool_name(raw_name)

    arg_keys = _WRITERS.get(name)
    command_triggers = registered_command_triggers(name)
    if (
        arg_keys is None
        and not command_triggers
        and not is_document_activity_tool(name)
    ):
        return None

    text = content or ""
    is_cc_builtin = raw_name in CC_BUILTIN_ALIASES
    _db = db or get_db()
    try:
        args = _find_call_args(
            _db,
            thread_id,
            tool_call_id,
            created_at,
            meta.get("assistant_step_id"),
        )
        if args is None:
            logger.warning(
                f"registry observer: no call record for tool result "
                f"{name}/{tool_call_id} in session {thread_id}; skipping"
            )
            return None
        args = resolve_dispatch(meta.get("name"), args)[1]
        # Presence is ephemeral and non-gating: its failure must never
        # suppress the durable registration below.
        try:
            activity = observe_document_activity(
                thread_id,
                name,
                args,
                text,
                bool(meta.get("is_error")),
                _db,
            )
        except Exception as error:
            logger.warning(
                f"document activity observation failed for {name}/{tool_call_id} "
                f"in session {thread_id}: {error}"
            )
            activity = None

        artifact = _artifact_from_result(
            thread_id, raw_name, name, args, text, meta, arg_keys,
            command_triggers, is_cc_builtin=is_cc_builtin, origin=origin, db=_db,
        )
        return observation_result(artifact, activity)
    finally:
        if not db:
            _db.close()


class CommandArtifactRegistrationError(RuntimeError):
    """A confirmed command artifact could not be bound to its thread."""

    def __init__(self, artifact: ObservedArtifactDecl, error: Exception) -> None:
        self.artifact = artifact
        self.error = error
        super().__init__(str(error))


def _has_active_shell_syntax(command: str) -> bool:
    """Detect top-level operators and active expansion without running a shell."""
    quote: str | None = None
    index = 0
    while index < len(command):
        char = command[index]
        if quote == "'":
            if char == "'":
                quote = None
            index += 1
            continue
        if char == "\\":
            if index + 1 >= len(command):
                index += 1
            elif quote == '"' and command[index + 1] not in '$`"\\':
                index += 1
            else:
                index += 2
            continue
        if char == "'" and quote is None:
            quote = "'"
            index += 1
            continue
        if char == '"':
            quote = None if quote == '"' else '"'
            index += 1
            continue
        if char == "`":
            return True
        if char == "$" and index + 1 < len(command):
            next_char = command[index + 1]
            if (
                next_char.isalpha()
                or next_char.isdigit()
                or next_char == "_"
                or next_char in _PARAMETER_START
            ):
                return True
        if quote is None and char in _SHELL_PUNCTUATION:
            return True
        index += 1
    return False


def command_matches_trigger(command: object, trigger: CommandTriggerDecl) -> bool:
    """Match one literal top-level command against a connector declaration."""
    if not isinstance(command, str) or "\n" in command or "\r" in command:
        return False
    if _has_active_shell_syntax(command):
        return False
    try:
        argv = shlex.split(command, comments=False, posix=True)
    except ValueError:
        return False
    prefix = trigger.argv_prefix
    return bool(prefix) and tuple(argv[: len(prefix)]) == prefix


def observe_command_result(
    thread_id: str,
    raw_tool_name: str | None,
    args: dict,
    content: str,
    *,
    is_error: bool = False,
    origin: RegistrationOrigin | None = None,
    db=None,
    notify: bool = False,
) -> dict | None:
    """Run connector-declared command triggers against one confirmed result."""
    if not thread_id or is_error:
        return None
    name = normalize_tool_name(raw_tool_name)
    observations: list[tuple[str, CommandTriggerDecl, ObservedArtifactDecl]] = []
    for connector, trigger in registered_command_triggers(name):
        command = args.get(trigger.command_arg)
        if not command_matches_trigger(command, trigger):
            continue
        artifact = trigger.extract_artifact(content)
        if artifact is not None:
            observations.append((connector, trigger, artifact))
    if not observations:
        return None
    if len(observations) != 1:
        names = ", ".join(
            f"{connector}:{trigger.name}" for connector, trigger, _ in observations
        )
        raise ValueError(f"ambiguous command artifact triggers: {names}")

    connector, trigger, artifact = observations[0]
    try:
        registered = register_observed_artifact(
            thread_id,
            integration=artifact.integration,
            type=artifact.type,
            external_id=artifact.external_id,
            title=artifact.title,
            path=artifact.path,
            metadata=artifact.metadata,
            origin=origin,
            db=db,
            notify=notify,
        )
    except Exception as error:
        raise CommandArtifactRegistrationError(artifact, error) from error
    if registered is not None:
        logger.info(
            "registry observer: {} trigger {} registered {} to {}",
            connector,
            trigger.name,
            artifact.external_id,
            thread_id,
        )
    return registered


def _call_args_from_metadata(
    metadata: dict | str | None,
    tool_call_id: str,
    *,
    require_unique: bool,
) -> dict | None:
    meta = _as_dict(metadata)
    calls = meta.get("llm_tool_calls") or meta.get("tool_calls") or []
    matches = [
        call for call in calls
        if isinstance(call, dict) and call.get("id") == tool_call_id
    ]
    if not matches:
        return None
    if require_unique and len(matches) != 1:
        raise ValueError(f"ambiguous authoritative tool call {tool_call_id!r}")
    call = matches[-1]
    function_value = call.get("function")
    function = function_value if isinstance(function_value, dict) else {}
    args = function.get("arguments", call.get("args"))
    if isinstance(args, dict):
        return args
    try:
        parsed = json.loads(args or "{}")
        return parsed if isinstance(parsed, dict) else None
    except (TypeError, ValueError):
        return None


def _find_call_args(
    db,
    thread_id: str,
    tool_call_id: str,
    created_at: str | None,
    assistant_step_id: str | None = None,
) -> dict | None:
    """Join a result row back to its matching ``llm_tool_calls`` record."""
    if assistant_step_id is not None:
        if not isinstance(assistant_step_id, str) or not assistant_step_id:
            raise ValueError("invalid authoritative Assistant-Step id")
        row = db.execute(
            "SELECT metadata FROM messages WHERE id = ? AND thread_id = ? "
            "AND role = 'assistant'",
            (assistant_step_id, thread_id),
        ).fetchone()
        if row is None:
            raise ValueError(
                f"missing authoritative Assistant Step {assistant_step_id!r}"
            )
        args = _call_args_from_metadata(
            row["metadata"], tool_call_id, require_unique=True,
        )
        if args is None:
            raise ValueError(
                f"Assistant Step {assistant_step_id!r} does not declare "
                f"tool call {tool_call_id!r}"
            )
        return args

    q = ("SELECT metadata FROM messages "
         "WHERE thread_id = ? AND role = 'assistant' AND metadata IS NOT NULL ")
    params: list = [thread_id]
    if created_at is not None:
        q += "AND created_at <= ? "
        params.append(created_at)
    q += "ORDER BY created_at DESC LIMIT ?"
    params.append(_CALL_SCAN_LIMIT)
    for r in db.execute(q, params):
        args = _call_args_from_metadata(
            r["metadata"], tool_call_id, require_unique=False,
        )
        if args is not None:
            return args
    return None


def _as_dict(metadata) -> dict:
    """Metadata arrives as a dict from live seams, JSON text from DB replay."""
    if isinstance(metadata, dict):
        return metadata
    if isinstance(metadata, str) and metadata:
        try:
            return json.loads(metadata)
        except ValueError:
            return {}
    return {}
