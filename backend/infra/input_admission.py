"""Transactional acceptance of one visible model-bound input."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from core.message_history import NON_HUMAN_MARKERS, REPLY_VIA_POST_UPDATE_KEY
from core.reply_context import REPLY_CONTEXT_KEY
from core.viewing_context import VIEWING_DOCUMENT_KEY, VIEWING_DOCUMENT_TARGET_KEY
from infra.database import get_db, write_transaction
from infra.document_viewing import PreparedViewingDocument
from infra.reply_context import validate_reply_context
from infra.thread_inputs import thread_admission_state
from infra.time import utc_now_iso
from infra.vendor_login import LOGIN_KEY, public_metadata

DELIVERY_INTENTS = frozenset({"apply_now", "queue_after_turn"})


def stamped_human_metadata(
    connection: sqlite3.Connection,
    role: str,
    message_id: str,
    metadata: dict | None,
    *,
    viewing_document: PreparedViewingDocument | None = None,
    thread_id: str | None = None,
) -> dict | None:
    """Return the metadata a newly admitted human message is stored with.

    A human message (role ``user`` with none of the agent, completion or event
    markers) gains the stamp that makes its model frame end with the
    ``post_update`` reply footer, a server-resolved canvas note if supplied and
    its validated reply context (what the person was answering, checked against
    ``thread_id``). All are written once, at admission, so a row
    admitted before the stamp existed keeps rendering as it was cached and
    every replay of a stamped row renders the same footer. A retry of an id
    admitted earlier keeps the payload it was stored with, stamped or not, so
    the retry stays a duplicate.
    """
    server_keys = (
        VIEWING_DOCUMENT_KEY, VIEWING_DOCUMENT_TARGET_KEY, REPLY_CONTEXT_KEY,
    )
    clean = (
        {key: value for key, value in metadata.items() if key not in server_keys}
        if metadata is not None and any(key in metadata for key in server_keys)
        else metadata
    )
    if role != "user" or any((clean or {}).get(key) for key in NON_HUMAN_MARKERS):
        return clean
    stored = connection.execute(
        "SELECT metadata FROM messages WHERE id=?", (message_id,),
    ).fetchone()
    prior = json.loads(stored["metadata"] or "{}") if stored is not None else None
    if prior is not None and REPLY_VIA_POST_UPDATE_KEY not in prior:
        return clean
    stamped = {**(clean or {}), REPLY_VIA_POST_UPDATE_KEY: True}
    if prior is not None:
        reply_context = prior.get(REPLY_CONTEXT_KEY)
    elif thread_id is not None:
        reply_context = validate_reply_context(
            connection, thread_id, (metadata or {}).get(REPLY_CONTEXT_KEY),
        )
    else:
        reply_context = None
    if reply_context:
        stamped[REPLY_CONTEXT_KEY] = reply_context
    if viewing_document is not None:
        target = viewing_document.target
        stamped[VIEWING_DOCUMENT_TARGET_KEY] = target
        # Reuse the admission snapshot on duplicate delivery, even if the file
        # was edited, moved or removed since. Changed targets still fail identity.
        note = (
            prior.get(VIEWING_DOCUMENT_KEY)
            if prior is not None and prior.get(VIEWING_DOCUMENT_TARGET_KEY) == target
            else viewing_document.note
        )
        if note:
            stamped[VIEWING_DOCUMENT_KEY] = note
    return stamped


@dataclass(frozen=True, slots=True)
class AcceptedInput:
    """One immutable FIFO position accepted by CORSIA."""

    thread_id: str
    message_id: str
    input_order: int
    revision: int
    intent: str
    target_execution_epoch: int
    target_model: str
    created_at: str
    duplicate: bool = False
    delivery_state: str = "queued"
    delivery_error: str | None = None


def _input_destination(
    state: sqlite3.Row, input_order: int,
) -> tuple[int, str]:
    """Route to the pending destination only while it can still resolve.

    A failed switch keeps its ``pending_model``/``pending_execution_epoch``
    for display and history, but that destination will never write again, so
    a new input must land on the source like the switch never happened.
    """
    boundary = state["switch_after_input_order"]
    pending_model = state["pending_model"]
    pending_epoch = state["pending_execution_epoch"]
    if (
        boundary is not None
        and pending_model is not None
        and pending_epoch is not None
        and state["switch_status"] != "failed"
        and input_order > int(boundary)
    ):
        return int(pending_epoch), str(pending_model)
    return int(state["execution_epoch"]), str(state["model"] or "").strip()


CHECKPOINT_CONFIGURATION_IMPOSSIBLE = "checkpoint_configuration_impossible"


def checkpoint_cap_refusal(model: str) -> tuple[str, str]:
    """Return the message and legal action when no checkpoint call can fit."""
    legal_action = "Raise the model's context cap or select another model."
    return f"The context cap for {model} is too small for a checkpoint. {legal_action}", legal_action


def checkpoint_cap_impossible(window: int, model: str) -> bool:
    """Return whether the cap alone, before any history, leaves no checkpoint room."""
    from core.fork_fit import ColdFoldInputTooLarge
    from core.fork_render import require_cold_fork_room
    from prompts.checkpoint import build_checkpoint_system_prompt

    try:
        require_cold_fork_room(
            window, build_checkpoint_system_prompt(), writer=model,
        )
    except ColdFoldInputTooLarge:
        return True
    return False


def _require_checkpoint_capacity(db: sqlite3.Connection, model: str) -> None:
    """Reject a proved impossible cap, without testing provider availability."""
    from infra.send_types import SendRejected
    from llm.context_window import get_context_window_or_none

    window = get_context_window_or_none(model, db=db)
    # Absence of discovery evidence is not proof of an impossible configuration.
    # Existing FIFO delivery owns temporary unavailability; do not probe here.
    if window is None or not checkpoint_cap_impossible(window, model):
        return
    message, legal_action = checkpoint_cap_refusal(model)
    raise SendRejected(
        409, message,
        metadata={
            "cause": CHECKPOINT_CONFIGURATION_IMPOSSIBLE,
            "legal_action": legal_action,
        },
    )


def input_delivery(message_id: str, *, db: sqlite3.Connection | None = None) -> tuple[str, str | None]:
    """Read an accepted input's durable delivery result after an earlier prefix advances."""
    connection = db if db is not None else get_db()
    try:
        row = connection.execute(
            "SELECT delivery_state,delivery_error FROM messages WHERE id=?",
            (message_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"accepted input disappeared: {message_id}")
        return str(row["delivery_state"]), row["delivery_error"]
    finally:
        if db is None:
            connection.close()


def _accepted_duplicate(
    connection: sqlite3.Connection,
    *,
    thread_id: str,
    role: str,
    content: str,
    intent: str,
    message_id: str,
    metadata: dict | None,
    delivery_options: dict | None,
) -> AcceptedInput | None:
    existing = connection.execute(
        "SELECT thread_id,role,content,metadata,input_order,delivery_intent,"
        "target_execution_epoch,target_model,delivery_revision,delivery_options,"
        "created_at,delivery_state,delivery_error FROM messages WHERE id=?",
        (message_id,),
    ).fetchone()
    if existing is None:
        return None
    stored_metadata = json.loads(existing["metadata"] or "{}")
    if isinstance(stored_metadata, dict):
        stored_metadata = public_metadata(stored_metadata)
    stored_options = json.loads(existing["delivery_options"] or "{}")
    same = (
        str(existing["thread_id"]) == thread_id
        and str(existing["role"]) == role
        and str(existing["content"]) == content
        and stored_metadata == (metadata or {})
        and stored_options == (delivery_options or {})
        and str(existing["delivery_intent"]) == intent
    )
    if not same:
        raise ValueError("input identity already has a different payload")
    return AcceptedInput(
        thread_id=thread_id,
        message_id=message_id,
        input_order=int(existing["input_order"]),
        revision=int(existing["delivery_revision"]),
        intent=intent,
        target_execution_epoch=int(existing["target_execution_epoch"]),
        target_model=str(existing["target_model"] or ""),
        created_at=str(existing["created_at"]),
        duplicate=True,
        delivery_state=str(existing["delivery_state"]),
        delivery_error=(
            str(existing["delivery_error"])
            if existing["delivery_error"] is not None else None
        ),
    )


def _validate_input_shape(metadata, intent, role) -> None:
    if metadata is not None and LOGIN_KEY in metadata:
        raise ValueError("reserved input metadata: _corsia_vendor_login")
    if intent not in DELIVERY_INTENTS:
        raise ValueError(f"invalid delivery intent: {intent}")
    if role not in {"user", "system"}:
        raise ValueError(f"invalid model-bound input role: {role}")


def accept_thread_input(
    thread_id: str,
    *,
    role: str,
    content: str,
    intent: str,
    message_id: str,
    metadata: dict | None = None,
    delivery_options: dict | None = None,
    continuation: dict | None = None,
    created_at: str | None = None,
    admit_while_fenced: bool = False,
    db: sqlite3.Connection | None = None,
) -> AcceptedInput:
    """Accept one input and freeze its FIFO and execution-epoch identity."""
    _validate_input_shape(metadata, intent, role)
    accepted_at = created_at or utc_now_iso()
    with write_transaction(db, scope="accept_thread_input") as connection:
        state = thread_admission_state(connection, thread_id, admit_while_fenced=admit_while_fenced)
        assert state is not None
        duplicate = _accepted_duplicate(
            connection,
            thread_id=thread_id,
            role=role,
            content=content,
            intent=intent,
            message_id=message_id,
            metadata=metadata,
            delivery_options=delivery_options,
        )
        if duplicate is not None:
            return duplicate
        if continuation is not None:
            from automation.continuation.validity import require_current

            require_current(thread_id, continuation, db=connection)
        input_order = int(state["next_input_order"])
        epoch, model = _input_destination(state, input_order)
        hold = _admission_hold(connection, model, thread_id=thread_id)
        from plans.public import schedule_for_accepted_input

        schedule_for_accepted_input(thread_id, db=connection)
        connection.execute(
            "INSERT INTO messages "
            "(id,thread_id,role,content,metadata,created_at,delivery_state,"
            "input_order,delivery_intent,target_execution_epoch,target_model,"
            "continuation_condition,delivery_options,write_outcome,delivery_error) "
            "VALUES (?,?,?,?,?,?,'queued',?,?,?,?,?,?,?,?)",
            (
                message_id, thread_id, role, content,
                json.dumps(metadata) if metadata else None,
                accepted_at,
                input_order,
                intent,
                epoch,
                model or None,
                json.dumps(continuation) if continuation else None,
                json.dumps(delivery_options) if delivery_options else None,
                "proved_no_write" if hold else None,
                hold,
            ),
        )
        connection.execute(
            "UPDATE threads SET next_input_order=next_input_order+1 WHERE id=?",
            (thread_id,),
        )
        # Accepting an input never invalidates the thread's idle marker; only
        # `start_idle_turn`, creating the Turn that supersedes it, may.
    return AcceptedInput(
        thread_id=thread_id,
        message_id=message_id,
        input_order=input_order,
        revision=1,
        intent=intent,
        target_execution_epoch=epoch,
        target_model=model,
        created_at=accepted_at,
        duplicate=False,
        delivery_error=hold,
    )


def _admission_hold(db: sqlite3.Connection, model: str, *, thread_id: str) -> str | None:
    """Check ``model`` can take the input, or name the hold for having none.

    A thread with no model to run on takes the FIFO-head hold
    ``unavailable_model_recovery`` uses, with its own cause: the input is
    kept, never failed or sent to a model nobody chose, and only the user
    choosing a model lifts it (``thread.model_row.select_first_model``).
    """
    if model:
        from connections.account_holds import model_account_hold

        hold = model_account_hold(thread_id, model, db=db)
        if hold:
            return hold
        _require_checkpoint_capacity(db, model)
        return None
    from infra.send_target import NO_MODEL_SELECTED

    return NO_MODEL_SELECTED
