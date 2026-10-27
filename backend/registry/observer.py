"""Artifact registration derived from persisted tool-call records."""

from __future__ import annotations

import json

from loguru import logger

from doceditor.presence import is_document_activity_tool, observe_document_activity

from events import connectors, observe_rules
from events.connectors import ObservedArtifactDecl
from infra.database import get_db
from infra.product_events import emit_product_event
from registry.observation_result import (
    ObservationResult,
    observation_result,
    observe_settled_tool_result,
)
from registry import models
from registry.command_match import command_matches_rule
from registry.observed_write import register_observed_write
from registry.registration_writer import RegistrationOrigin
from registry.service import register_observed_artifact
from tools.catalog import CC_BUILTIN_ALIASES, normalize_tool_name
from tools.mcp_stamp import APP_FIELD, TOOL_FIELD
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
    thread_id, name, args, text, meta, arg_keys,
    *, is_cc_builtin, origin, db,
) -> dict | None:
    """Preserve existing registration behavior beside activity observation."""
    if arg_keys is None:
        return None
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
    if arg_keys is None and not is_document_activity_tool(name):
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
            thread_id, name, args, text, meta, arg_keys,
            is_cc_builtin=is_cc_builtin, origin=origin, db=_db,
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


def _record(db, thread_id, rule, outcome, artifact, reason, told) -> None:
    saved = observe_rules.record(db, thread_id, rule, outcome, artifact and {
        "integration": artifact.integration, "type": artifact.type,
        "external_id": artifact.external_id}, reason)
    if not saved and told is not None:
        told.append("Couldn't save this result to the rule's history, so `explain` won't show this run.")
    emit_product_event("event_rule", outcome, thread_id=thread_id, properties={
        "rule_id": rule.id, "kind": observe_rules.OBSERVE, "connector": rule.connector,
        "shipped": rule.origin.startswith(observe_rules.RULE_SET_ORIGIN), "reason": reason})


def _extractor(rule, db, thread_id, told):
    extract = connectors.artifact_extractor(rule.connector, rule.item_type)
    if extract is None:
        reason = f"connector {rule.connector} cannot read {rule.item_type.replace('-', ' ')} from command output"
        logger.error("registry observer: rule {}: {}", rule.id, reason)
        if told is not None:
            told.append(f"Not registered: {reason}.")
        _record(db, thread_id, rule, "not_registered", None, reason, told)
    return extract


def _already_registered(db, thread_id, rule, artifact, label, told) -> None:
    current = models.resolve(artifact.integration, artifact.type, artifact.external_id, db=db)
    owns = bool(current) and current["parent_thread_id"] == thread_id
    if told is not None:
        told.append(f"{label} is already registered to you." if owns else
                    f"Not registered to you: {label} belongs to another chat; you already follow it.")
    _record(db, thread_id, rule, "registered" if owns else "not_registered", artifact,
            "it was already registered to you" if owns else "you already follow it", told)


def _extract_observation(rule, args, content, app, db, thread_id, told):
    if app is not None:
        if not connectors.app_matches(app, rule.connector):
            return False, None
        extract = _extractor(rule, db, thread_id, told)
        return (True, extract(content)) if extract else (False, None)
    if not command_matches_rule(args.get(rule.command_arg), rule):
        return False, None
    extract = _extractor(rule, db, thread_id, told)
    return (True, extract(content)) if extract else (False, None)


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
    changed: list[dict] | None = None,
    metadata: dict | None = None,
) -> dict | None:
    """Apply observe rules to a confirmed shell or stamped connected-app result.

    Returns the artifact or ``None``; appends what happened to ``told`` and records it for ``explain``.
    """
    metadata = metadata or {}
    app, upstream = metadata.get(APP_FIELD), metadata.get(TOOL_FIELD)
    connected = isinstance(app, str) and bool(app) and isinstance(upstream, str) and bool(upstream)
    name = upstream if connected else normalize_tool_name(raw_tool_name)
    if not thread_id or is_error or (not connected and name not in observe_rules.SHELL_TOOLS):
        return None
    rule_db = db or get_db()
    try:
        project_id = observe_rules.project_of(rule_db, thread_id)
        covering = observe_rules.applicable(rule_db, project_id, thread_id, name)
    finally:
        if not db:
            rule_db.close()
    owned, followed = None, None  # the owner's registration first, else a follower's
    seen: set[tuple[str, str, str]] = set()
    for rule in covering:
        matched, artifact = _extract_observation(
            rule, args, content, app if connected else None, db, thread_id, told)
        if not matched:
            continue
        item_name = rule.item_type.replace("-", " ")
        if artifact is None:
            if told is not None:
                source = "app result" if connected else "command's output"
                told.append(f"Not registered: the {source} named no {item_name}.")
            _record(db, thread_id, rule, "not_registered", None, f"the output named no {item_name}", told)
            continue
        key = (artifact.integration, artifact.type, artifact.external_id)
        if key in seen:
            _record(db, thread_id, rule, "not_registered", artifact,
                    f"another rule handled this {item_name} in the same run", told)
            continue
        seen.add(key)
        try:
            registered = register_observed_artifact(
                thread_id, integration=artifact.integration, type=artifact.type,
                external_id=artifact.external_id, title=artifact.title, path=artifact.path,
                metadata=artifact.metadata, origin=origin, db=db, notify=notify,
            )
        except Exception as error:
            _record(db, thread_id, rule, "not_registered", artifact, str(error), None)
            raise CommandArtifactRegistrationError(artifact, error) from error
        if registered is not None and changed is not None:
            changed.append(registered)
        label = f"{artifact.type.replace('-', ' ')} {artifact.external_id}"
        if registered is None:  # this thread already has a current registration: owner or follower
            _already_registered(db, thread_id, rule, artifact, label, told)
        elif registered.get("parent_thread_id") == thread_id:
            if told is not None:
                told.append(f"Registered {label} to you.")
            _record(db, thread_id, rule, "registered", artifact, None, told)
            owned = owned or registered
            logger.info("registry observer: rule {} registered {} to {}", rule.id, artifact.external_id, thread_id)
        else:
            if told is not None:
                told.append(f"Not registered to you: {label} already belongs to another chat, "
                            f"which keeps it; you follow it.")
            _record(db, thread_id, rule, "not_registered", artifact, "it already belongs to another chat", told)
            followed = followed or registered
    return owned or followed


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
