"""Artifact registration derived from persisted tool-call records."""

from __future__ import annotations

import json

from loguru import logger

from doceditor.presence import is_document_activity_tool, observe_document_activity

from events import connectors, observe_rules
from events.connectors import ObservedArtifactDecl
from infra.database import get_db
from registry.observation_result import (
    ObservationResult,
    observation_result,
    observe_settled_tool_result,
)
from registry.command_match import command_matches_trigger  # noqa: F401  (re-exported)
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
    thread_id, raw_name, name, args, text, meta, arg_keys, is_shell,
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
        ) if is_shell else None
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
    is_shell = name in observe_rules.SHELL_TOOLS
    if (
        arg_keys is None
        and not is_shell
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
                call_id=tool_call_id,
            )
        except Exception as error:
            logger.warning(
                f"document activity observation failed for {name}/{tool_call_id} "
                f"in session {thread_id}: {error}"
            )
            activity = None

        artifact = _artifact_from_result(
            thread_id, raw_name, name, args, text, meta, arg_keys,
            is_shell, is_cc_builtin=is_cc_builtin, origin=origin, db=_db,
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


def _record(thread_id, rule, outcome, artifact, reason) -> None:
    observe_rules.record(thread_id, rule, outcome, artifact and {
        "integration": artifact.integration, "type": artifact.type,
        "external_id": artifact.external_id}, reason)


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
    told: list[str] | None = None,
) -> dict | None:
    """Run the observe rules that cover this thread against one confirmed shell result.

    Returns the registered artifact, or ``None``; what happened is appended to ``told`` (when
    given) in words for the caller, and recorded for ``explain``.
    """
    name = normalize_tool_name(raw_tool_name)
    if not thread_id or is_error or name not in observe_rules.SHELL_TOOLS:
        return None
    rule_db = db or get_db()
    try:
        covering = observe_rules.applicable(rule_db, thread_id, name)
    finally:
        if not db:
            rule_db.close()
    first: dict | None = None
    seen: set[tuple[str, str, str]] = set()
    for rule in covering:
        if not command_matches_trigger(args.get(rule.command_arg), rule):
            continue
        extract = connectors.artifact_extractor(rule.connector, rule.item_type)
        artifact = extract(content) if extract else None
        if artifact is None:
            _record(thread_id, rule, "not_registered", None, f"the output named no {rule.item_type}")
            if told is not None:
                told.append(f"Not registered: the command's output named no {rule.item_type}.")
            continue
        key = (artifact.integration, artifact.type, artifact.external_id)
        if key in seen:
            continue
        seen.add(key)
        try:
            registered = register_observed_artifact(
                thread_id, integration=artifact.integration, type=artifact.type,
                external_id=artifact.external_id, title=artifact.title, path=artifact.path,
                metadata=artifact.metadata, origin=origin, db=db, notify=notify,
            )
        except Exception as error:
            _record(thread_id, rule, "not_registered", artifact, str(error))
            raise CommandArtifactRegistrationError(artifact, error) from error
        label = f"{artifact.type} {artifact.external_id}"
        if registered is None:
            if told is not None:
                told.append(f"{label} was already registered to you.")
        elif registered.get("parent_thread_id") == thread_id:
            _record(thread_id, rule, "registered", artifact, None)
            if told is not None:
                told.append(f"Registered {label} to you.")
            first = first or registered
            logger.info("registry observer: rule {} registered {} to {}", rule.id, artifact.external_id, thread_id)
        else:
            _record(thread_id, rule, "not_registered", artifact, "it already belongs to another chat")
            if told is not None:
                told.append(f"Not registered to you: {label} already belongs to another chat, "
                            f"which keeps it; you follow it.")
    return first


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
