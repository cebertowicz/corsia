"""CC child-session persistence.

A delegated CC subagent is a child session rendered in the same chat canvas as
every other conversation. Its transcript uses the canonical message shape:
assistant rows carry ``metadata.llm_tool_calls`` and ``metadata.reasoning``;
tool results are separate ``role=tool`` rows naming both the tool call and its
emitting Assistant Step. ``llm_to_display`` joins those child rows by lineage.

Child sessions get ``spawned_by`` set to their parent, which is the single
source of truth for subagent-ness: the sidebar's ``spawned_by IS NULL`` filter
excludes them; they're reached via the inline chip in the parent thread.
"""

from __future__ import annotations

import json
import hashlib
import sqlite3
import uuid
from datetime import datetime, timezone

from core.reasoning_provenance import normalize_reasoning_provenance
from infra.database import get_db, write_transaction
from infra.fresh_threads import create_fresh_thread
from infra.queries import assert_turn_accepts_tool_calls, insert_message
from subagents.assistant_step_display import load_assistant_step_display
from subagents.model_display_name import external_model_display_name


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolved_external_model_id(provider: str, model: str | None) -> str | None:
    """Normalize external harness aliases to the selected provider/model id."""
    if not model:
        return None
    if "/" in model:
        return model
    if provider == "codex":
        return f"codex/{model}"
    return f"claude-code/{model}"


def provider_message_metadata(
    provider: str,
    *,
    model: str | None = None,
    model_name: str | None = None,
    expected_model: str | None = None,
) -> dict:
    """Return external-harness metadata using provider-reported model truth.

    ``model`` is what the harness reported, never the selected session label.
    ``expected_model`` is the discovery row's resolved identity. Keeping those
    facts separate makes a provider reroute or missing identity explicit while
    ``sessions.model`` remains the sole owner of the user's saved selection.
    """
    # ``cc`` means Claude Code, not "any external harness"; Codex rows carry
    # their own marker so transcript consumers do not conflate providers.
    meta = {"codex": True, "harness": "codex"} if provider == "codex" else {
        "cc": True,
        "harness": "claude-code",
    }
    model_id = resolved_external_model_id(provider, model)
    if model_id:
        meta["model"] = model_id
        meta["model_name"] = model_name or external_model_display_name(model_id)
        meta["provider_reported_model"] = model_id
    expected_id = resolved_external_model_id(provider, expected_model)
    if expected_id:
        meta["expected_provider_model"] = expected_id
        if model_id:
            meta["model_identity_mismatch"] = model_id != expected_id
        else:
            meta["provider_reported_model_missing"] = True
    return meta



def corsia_tool_call_id(
    thread_id: str,
    *,
    provider_tool_id: str | None,
    name: str | None,
) -> str | None:
    """Return the stable CORSIA id for one provider-observed tool call."""
    if not provider_tool_id:
        return None
    raw = f"{thread_id}\0{provider_tool_id}\0{name or ''}"
    return "mtc_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


_EXTERNAL_MESSAGE_ID_NAMESPACE = uuid.UUID("6f57e8c7-206e-525d-bbf8-e09bcd1996be")


def _external_message_id(legacy_message_id: str) -> str:
    """Map one legacy replay key to its canonical UUIDv5 message identity."""
    return str(uuid.uuid5(_EXTERNAL_MESSAGE_ID_NAMESPACE, legacy_message_id))


def _turn_binding_meta(
    turn_id: str | None,
    turn_unbound_reason: str | None,
) -> dict[str, str]:
    """Return one explicit live or historical-import Turn binding."""
    if isinstance(turn_id, str) and turn_id:
        if turn_unbound_reason is not None:
            raise ValueError("bound rows cannot also declare an unbound reason")
        return {"turn_id": turn_id}
    if turn_unbound_reason != "legacy_import":
        raise ValueError(
            "live message persistence requires a non-empty Turn ID"
        )
    return {"turn_unbound_reason": "legacy_import"}


def _insert_message_once(
    thread_id: str,
    role: str,
    content: str,
    metadata: dict,
    *,
    msg_id: str,
    legacy_msg_id: str | None = None,
    db=None,
    created_at: str | None = None,
) -> str:
    """Insert one replay-stable row, discovering its pre-UUID legacy identity."""
    owns_db = db is None
    _db = get_db() if owns_db else db
    if owns_db and _db.in_transaction:
        raise RuntimeError(
            "owned replay insert requires a clean pooled connection"
        )

    def _find_existing() -> str | None:
        candidate_ids = [msg_id]
        if legacy_msg_id and legacy_msg_id != msg_id:
            candidate_ids.append(legacy_msg_id)
        placeholders = ",".join("?" for _ in candidate_ids)
        rows = _db.execute(
            f"SELECT id FROM messages WHERE id IN ({placeholders})",
            candidate_ids,
        ).fetchall()
        existing_ids = {str(row[0]) for row in rows}
        return next(
            (candidate for candidate in candidate_ids if candidate in existing_ids),
            None,
        )

    try:
        with write_transaction(_db, scope="insert_replay_message"):
            existing_id = _find_existing()
            if existing_id is not None:
                return existing_id
            try:
                insert_message(
                    thread_id,
                    role,
                    content,
                    metadata,
                    msg_id=msg_id,
                    db=_db,
                    created_at=created_at,
                )
            except sqlite3.IntegrityError:
                existing_id = _find_existing()
                if existing_id is None:
                    raise
                return existing_id
            return msg_id
    finally:
        if owns_db:
            _db.close()


def create_child_thread(
    *, user_id: str, parent_thread_id: str, thread_id: str | None = None,
    title: str | None = None, model: str, project_id: str | None = None,
    account_id: str | None = None, db=None,
) -> str:
    """Create a hidden child via the shared scoped creation transaction."""
    sid = thread_id or str(uuid.uuid4())
    create_fresh_thread(
        user_id=user_id, thread_id=sid, title=title or "Subagent", model=model,
        project_id=project_id, spawned_by=parent_thread_id,
        parent_thread_id=parent_thread_id, created_at=_now(),
        account_id=account_id, actor_thread_id=parent_thread_id, db=db,
    )
    return sid


def persist_assistant_step(
    child_thread_id: str,
    *,
    turn_id: str | None,
    turn_unbound_reason: str | None = None,
    content: str,
    tool_uses: list[dict] | None = None,
    reasoning: str | None = None,
    reasoning_provenance: dict | None = None,
    db=None,
    created_at: str | None = None,
    provider: str = "cc",
    model: str | None = None,
    expected_model: str | None = None,
    provider_turn_id: str | None = None,
    provider_message_id: str | None = None,
) -> str:
    """Persist one provider Assistant Step, optionally with a tool batch."""
    meta: dict = provider_message_metadata(
        provider, model=model, expected_model=expected_model,
    )
    if provider_turn_id is not None:
        meta["provider_turn_id"] = provider_turn_id
    if isinstance(provider_message_id, str) and provider_message_id:
        meta["provider_message_id"] = provider_message_id
    uses = tool_uses or []
    if uses:
        meta["step_type"] = "tool_calls"
        meta["llm_tool_calls"] = [
            {
                "id": tool.get("id"),
                "type": "function",
                "function": {
                    "name": tool.get("name"),
                    "arguments": json.dumps(tool.get("arguments") or {}),
                },
            }
            for tool in uses
        ]
        meta["tool_hints"] = [
            tool.get("hint") or tool.get("name") for tool in uses
        ]
    if reasoning:
        meta["reasoning"] = reasoning
    if reasoning or reasoning_provenance is not None:
        meta["reasoning_provenance"] = normalize_reasoning_provenance(
            reasoning_provenance
        )
    meta.update(_turn_binding_meta(turn_id, turn_unbound_reason))
    legacy_msg_id: str | None = None
    if len(uses) == 1:
        only = uses[0]
        mtc_id = corsia_tool_call_id(
            child_thread_id,
            provider_tool_id=only.get("id"),
            name=only.get("name"),
        )
        if mtc_id:
            meta["corsia_tool_call_id"] = mtc_id
            meta["provider_tool_call_id"] = only.get("id")
            legacy_msg_id = f"{mtc_id}_call"
    if legacy_msg_id is None and uses and all(
        isinstance(tool.get("id"), str) and tool["id"] for tool in uses
    ):
        batch_key = "\0".join(str(tool["id"]) for tool in uses)
        digest = hashlib.sha256(
            f"{child_thread_id}\0assistant-step\0{batch_key}".encode("utf-8")
        ).hexdigest()[:24]
        legacy_msg_id = f"mas_{digest}"
    if legacy_msg_id is not None:
        return _insert_message_once(
            child_thread_id,
            "assistant",
            content,
            meta,
            msg_id=_external_message_id(legacy_msg_id),
            legacy_msg_id=legacy_msg_id,
            db=db,
            created_at=created_at,
        )
    return insert_message(
        child_thread_id,
        "assistant",
        content,
        meta,
        msg_id=str(uuid.uuid4()),
        db=db,
        created_at=created_at,
    )


def persist_assistant_text(
    child_thread_id: str,
    text: str,
    *,
    turn_id: str | None,
    turn_unbound_reason: str | None = None,
    reasoning: str | None = None,
    reasoning_provenance: dict | None = None,
    db=None,
    created_at: str | None = None, provider: str = "cc",
    model: str | None = None,
    expected_model: str | None = None,
    provider_turn_id: str | None = None,
    provider_message_id: str | None = None,
) -> str:
    """Persist a text-only Assistant Step."""
    return persist_assistant_step(
        child_thread_id,
        turn_id=turn_id,
        turn_unbound_reason=turn_unbound_reason,
        content=text,
        reasoning=reasoning,
        reasoning_provenance=reasoning_provenance,
        db=db,
        created_at=created_at,
        provider=provider,
        model=model,
        expected_model=expected_model,
        provider_turn_id=provider_turn_id,
        provider_message_id=provider_message_id,
    )


def _append_tool_use_to_step(
    child_thread_id: str,
    assistant_step_id: str,
    *,
    turn_id: str | None,
    tool_id: str,
    name: str,
    arguments: dict,
    hint: str | None,
    provider_turn_id: str,
    db,
) -> str:
    """Append one declared call to an existing Step, atomically with its guard.

    The admission check and the UPDATE it guards must be ONE transaction, which
    is what ``assert_turn_accepts_tool_calls`` documents and what
    ``insert_message`` already does for the INSERT shape. Run apart, they leave
    exactly the window the check exists to close: the owning Turn's terminal
    commits between them, and the UPDATE then lands a declared call on a Turn
    that has already settled — owed a result no terminal will ever write.
    Holding the write lock from before the read makes that ordering
    unrepresentable rather than unlikely.
    """
    from infra.database import write_transaction

    # Ownership is declared, not guessed: an idle connection gets its own
    # BEGIN IMMEDIATE and commit (the caller handed one over and got a durable
    # write before, so it must still get one), and a connection already inside
    # a caller's transaction nests on a savepoint and commits with it.
    with write_transaction(db, scope="append_tool_use") as connection:
        return _append_tool_use_locked(
            child_thread_id,
            assistant_step_id,
            turn_id=turn_id,
            tool_id=tool_id,
            name=name,
            arguments=arguments,
            hint=hint,
            provider_turn_id=provider_turn_id,
            db=connection,
        )


def _append_tool_use_locked(
    child_thread_id: str,
    assistant_step_id: str,
    *,
    turn_id: str | None,
    tool_id: str,
    name: str,
    arguments: dict,
    hint: str | None,
    provider_turn_id: str,
    db,
) -> str:
    # The other declaration shape: this appends a call to an existing Step by
    # UPDATE, so it never reaches insert_message's admission check, and it
    # creates exactly the same debt an INSERT would.
    if turn_id is not None:
        assert_turn_accepts_tool_calls(db, turn_id)
    row = db.execute(
        "SELECT metadata FROM messages WHERE id=? AND thread_id=? "
        "AND role='assistant'",
        (assistant_step_id, child_thread_id),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"missing Assistant Step {assistant_step_id!r}")
    metadata = json.loads(row["metadata"] or "{}")
    if metadata.get("turn_id") != turn_id:
        raise RuntimeError("Assistant Step belongs to a different Turn")
    if metadata.get("provider_turn_id") != provider_turn_id:
        raise RuntimeError("Assistant Step belongs to a different provider turn")
    calls = list(metadata.get("llm_tool_calls") or [])
    candidate = {
        "id": tool_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments or {}),
        },
    }
    matches = [call for call in calls if call.get("id") == tool_id]
    if matches:
        if len(matches) == 1 and matches[0] == candidate:
            return assistant_step_id
        raise RuntimeError("provider tool call identity changed within Assistant Step")
    calls.append(candidate)
    metadata["step_type"] = "tool_calls"
    metadata["llm_tool_calls"] = calls
    metadata["tool_hints"] = [
        *list(metadata.get("tool_hints") or []),
        hint or name,
    ]
    updated = db.execute(
        "UPDATE messages SET metadata=? WHERE id=? AND thread_id=? "
        "AND role='assistant' AND metadata=?",
        (
            json.dumps(metadata),
            assistant_step_id,
            child_thread_id,
            row["metadata"],
        ),
    )
    if updated.rowcount != 1:
        raise RuntimeError("Assistant Step tool-call append lost CAS ownership")
    return assistant_step_id


def persist_tool_use(
    child_thread_id: str,
    *,
    turn_id: str | None,
    turn_unbound_reason: str | None = None,
    tool_id: str,
    name: str,
    arguments: dict,
    hint: str | None = None,
    reasoning: str | None = None,
    reasoning_provenance: dict | None = None,
    provider_turn_id: str | None = None,
    db=None,
    created_at: str | None = None,
    provider: str = "cc",
    model: str | None = None,
    expected_model: str | None = None,
) -> str:
    """Persist a tool call as a canonical intermediate assistant row.

    Shape matches the main chat's LLM format: empty content plus
    ``metadata.llm_tool_calls`` and ``step_type='tool_calls'``. The row remains
    its own durable Assistant Step and owns its result lineage.
    """
    with write_transaction(db, scope="persist_provider_tool_use") as connection:
        if provider_turn_id is not None:
            rows = connection.execute(
                "SELECT id FROM messages INDEXED BY idx_messages_turn_calls "
                "WHERE thread_id=? AND role='assistant' "
                "AND json_valid(metadata) "
                "AND json_extract(metadata,'$.turn_id')=? "
                "AND json_extract(metadata,'$.provider_turn_id')=? "
                "ORDER BY seq DESC LIMIT 1",
                (child_thread_id, turn_id, provider_turn_id),
            ).fetchall()
            if rows:
                return _append_tool_use_to_step(
                    child_thread_id,
                    str(rows[0]["id"]),
                    turn_id=turn_id,
                    tool_id=tool_id,
                    name=name,
                    arguments=arguments,
                    hint=hint,
                    provider_turn_id=provider_turn_id,
                    db=connection,
                )
        return persist_assistant_step(
            child_thread_id,
            turn_id=turn_id,
            turn_unbound_reason=turn_unbound_reason,
            content="",
            tool_uses=[{
                "id": tool_id,
                "name": name,
                "arguments": arguments,
                "hint": hint or name,
            }],
            reasoning=reasoning,
            reasoning_provenance=reasoning_provenance,
            db=connection,
            created_at=created_at,
            provider=provider,
            model=model,
            expected_model=expected_model,
            provider_turn_id=provider_turn_id,
        )


def persist_legacy_import_tool_result(
    child_thread_id: str,
    *,
    tool_use_id: str,
    name: str,
    content: str,
    is_error: bool = False,
    assistant_step_id: str | None = None,
    db=None,
    created_at: str | None = None,
) -> str:
    """Reconstruct one explicitly historical, Turn-unbound transcript result."""
    from core.tool_result_projection import tool_result_integrity_metadata

    meta: dict = {
        "tool_call_id": tool_use_id,
        "provider_tool_call_id": tool_use_id,
        "name": name,
        **tool_result_integrity_metadata(content),
        **_turn_binding_meta(None, "legacy_import"),
    }
    if assistant_step_id:
        meta["assistant_step_id"] = assistant_step_id
    if is_error:
        meta["is_error"] = True
    mtc_id = corsia_tool_call_id(
        child_thread_id,
        provider_tool_id=tool_use_id,
        name=name,
    )
    if mtc_id:
        meta["corsia_tool_call_id"] = mtc_id
        legacy_msg_id = f"{mtc_id}_result"
        return _insert_message_once(
            child_thread_id,
            "tool",
            content,
            meta,
            msg_id=_external_message_id(legacy_msg_id),
            legacy_msg_id=legacy_msg_id,
            db=db,
            created_at=created_at,
        )
    return insert_message(
        child_thread_id,
        "tool",
        content,
        meta,
        msg_id=str(uuid.uuid4()),
        db=db,
        created_at=created_at,
    )


def persist_tool_result(
    child_thread_id: str, *, turn_id: str | None,
    turn_unbound_reason: str | None = None,
    tool_use_id: str, name: str, content: str,
    is_error: bool = False, assistant_step_id: str | None = None,
    synthetic_tool_result: bool = False,
    synthetic_reason: str | None = None,
    _verified_assistant_step_metadata: object | None = None,
    db=None, created_at: str | None = None,
) -> str:
    """Delegate every canonical tool-result write to the sole delivery transition."""
    del created_at
    if turn_unbound_reason is not None:
        raise ValueError("tool results require a bound live Turn")
    from core.tool_result_delivery import (
        persist_tool_execution_result,
        synthetic_result_identity,
    )

    if synthetic_tool_result:
        invocation_id, extra_metadata = synthetic_result_identity(
            tool_use_id=tool_use_id, reason=synthetic_reason,
        )
    else:
        invocation_id = f"provider:{tool_use_id}"
        extra_metadata = {}
    owns_db = db is None
    _db = get_db() if owns_db else db
    if owns_db and _db.in_transaction:
        raise RuntimeError(
            "owned replay insert requires a clean pooled connection"
        )
    try:
        with write_transaction(_db, scope="persist_child_tool_result"):
            result = persist_tool_execution_result(
                thread_id=child_thread_id,
                turn_id=turn_id or "",
                invocation_id=invocation_id,
                assistant_step_id=assistant_step_id,
                provider_tool_call_id=tool_use_id,
                name=name,
                arguments=None,
                content=content,
                is_error=is_error,
                extra_metadata=extra_metadata,
                _verified_assistant_step_metadata=_verified_assistant_step_metadata,
                db=_db,
            )
            return result.message_id
    finally:
        if db is None:
            _db.close()


def record_seed(child_thread_id: str, seed_text: str, db=None) -> str:
    """Persist the canonical visibility projection of a CC spawn seed.

    Exact provider-bound seed bytes remain in the context-boundary source
    artifact; role-only conductor templates are intentionally not messages.
    """
    return insert_message(
        child_thread_id, "system", seed_text, {"cc": True, "type": "seed"},
        msg_id=str(uuid.uuid4()), db=db,
    )


def record_user_frame(
    child_thread_id: str, text: str, db=None, created_at: str | None = None,
    *, turn_id: str | None,
    turn_unbound_reason: str | None = None,
    edited: bool = False, extra_meta: dict | None = None,
) -> str:
    """Persist a user frame fed into the subagent (the task / reply-into).

    ``edited`` tags the row as an edit-and-resend (parity with main-chat and
    non-CC conversation edits, which set ``metadata.edited``) so the canvas
    renders the "edited" indicator and the session metadata is correct.
    ``extra_meta`` merges additional inbound-source keys into the row metadata
    (e.g. sender attribution from a delegation send or ``routed_event`` from
    event delivery). Never mutates the base ``{"cc": True}`` shape.
    """
    meta: dict = {"cc": True}
    if edited:
        meta["edited"] = True
    if extra_meta:
        meta.update(extra_meta)
    meta.update(_turn_binding_meta(turn_id, turn_unbound_reason))
    return insert_message(child_thread_id, "user", text, meta,
                          msg_id=str(uuid.uuid4()), db=db, created_at=created_at)
