"""Projection of durable message rows into provider-neutral messages."""

from __future__ import annotations

import json
from typing import Any

from loguru import logger

from core.reasoning_provenance import normalize_reasoning_provenance
from history.message_attachments import attachments as _attachments
from history.types import PortableHistoryError, PortableMessage
from infra.turn_provenance import (
    UNREADABLE_METADATA_REASON,
    UNUSABLE_TURN_PROVENANCE_REASON,
    TurnProvenanceError,
    turn_provenance,
)


_INBOUND_SOURCE_KEYS = (
    "sender_kind",
    "from_thread",
    "from_title",
    "scope",
    "routed_event",
    "plan_paused_since",
    "reply_via_post_update",
    "viewing_document",
)


def _metadata(raw: Any, *, message_id: str, role: str) -> dict[str, Any]:
    """Read one row's metadata. Only a TOOL row's is allowed to refuse.

    A tool row's metadata IS its binding — which call it answers, under which
    Step — so an unreadable one is a row that cannot be placed, and the strict
    failure is what drives the established quarantine repair. Every other role's
    metadata is descriptive: an unreadable one costs its own annotations, not
    the thread's ability to be replayed, switched, or folded. Raising there
    made one corrupt blob permanently fatal to a whole thread, with no repair
    path at all, which is the wedge shape this ledger exists to remove.
    """
    if raw in (None, ""):
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    parsed: Any = None
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            if role == "tool":
                raise PortableHistoryError(
                    f"message {message_id!r} has malformed metadata"
                ) from exc
            parsed = None
        if isinstance(parsed, dict):
            return parsed
    if role == "tool":
        raise PortableHistoryError(f"message {message_id!r} metadata is not an object")
    logger.warning(
        "portable history: message {} has unreadable metadata; projecting it "
        "as a row bound to no Turn",
        message_id,
    )
    # Says what is true rather than dropping the annotations silently: this row
    # cannot be bound to a Turn. That is the same statement the pre-binding
    # migration stamps on rows it could not bind, so the projection's existing
    # unbound-Turn path carries it — no second concept, and no invented
    # ``turn_id`` that would put the row inside a Turn it may not belong to.
    return {"turn_unbound_reason": UNREADABLE_METADATA_REASON}


def _turn_provenance(row: dict[str, Any]) -> tuple[str | None, str | None]:
    """Project unusable durable provenance as an unbound row and report it."""
    try:
        return turn_provenance(row)
    except TurnProvenanceError as exc:
        if row["role"] == "tool":
            raise PortableHistoryError(str(exc)) from exc
        logger.warning(
            "portable history: message {} has unusable Turn provenance ({}); "
            "projecting it as a row bound to no Turn",
            row["id"],
            exc,
        )
        return None, UNUSABLE_TURN_PROVENANCE_REASON


def _tool_calls(row: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    """Project the calls a Step declared, omitting any that cannot be replayed.

    A declaration is provider bytes, so its shape is not something the thread's
    usability may depend on. A call with no id, a duplicate id, or an entry
    that is not an object cannot be paired with a result — by the provider
    either, which rejects a tool_use without a matching tool_result — so
    raising here did not protect the replay, it only made every switch, fold
    and cold rebuild of that thread impossible. Forever: nothing repairs a
    declaration already durable in the transcript.

    Omitted, named, and reported instead: the Assistant Step's own bytes stay
    verbatim in the durable transcript, the warning names what was dropped, and
    ``core.tool_result_debt.tool_debt_report`` reports it through
    ``violations()`` — the same shape the contract already requires of one
    unsupported checkpoint citation, which must not destroy valid continuity.
    """
    raw_calls = (
        row["_meta"].get("llm_tool_calls") or row["_meta"].get("tool_calls") or []
    )
    if not isinstance(raw_calls, list):
        logger.warning(
            "portable history: Assistant Step {} declares a non-list of tool "
            "calls; projecting the Step with none",
            row["id"],
        )
        return ()
    # ONE definition of "an id a result can bind to", shared with the settler
    # that writes those results. Two definitions was not a style problem: the
    # settler paid an integer id as "7" while this reader dropped it, so the
    # row it had just written declared nothing here and every later projection
    # of that thread raised forever.
    from core.tool_result_debt import declared_call_id

    calls: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_calls:
        call_id = declared_call_id(raw)
        if call_id is None or call_id in seen:
            # ``repr``, bounded: an id may hold a lone UTF-16 surrogate a
            # strict log sink cannot encode, and has no length limit either.
            logger.warning(
                "portable history: Assistant Step {} declares a call with no "
                "usable identity ({}); omitting it from the replay",
                row["id"],
                repr(raw.get("id") if isinstance(raw, dict) else raw)[:120],
            )
            continue
        call = dict(raw)
        call["id"] = call_id
        seen.add(call_id)
        function = call.get("function")
        if not isinstance(function, dict):
            arguments = call.get("args", "")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, sort_keys=True)
            call = {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": call.get("name", ""),
                    "arguments": arguments,
                },
            }
        calls.append(call)
    return tuple(calls)


def _nonempty_str(meta: dict[str, Any], key: str) -> str | None:
    value = meta.get(key)
    return value if isinstance(value, str) and value else None


def _portable_message(
    row: dict[str, Any],
    turn_id: str | None,
    *,
    tool_calls: tuple[dict[str, Any], ...] = (),
    db=None,
) -> PortableMessage:
    meta = row["_meta"]
    provenance: dict[str, Any] = {}
    reasoning_text: str | None = None
    if row["role"] == "assistant" and (
        meta.get("reasoning") is not None
        or meta.get("reasoning_provenance") is not None
    ):
        provenance = normalize_reasoning_provenance(meta.get("reasoning_provenance"))
        raw_reasoning = meta.get("reasoning")
        reasoning_text = (
            raw_reasoning if isinstance(raw_reasoning, str) and raw_reasoning else None
        )
    inbound_source = {
        key: meta[key]
        for key in _INBOUND_SOURCE_KEYS
        if key in meta and meta[key] is not None
    }
    return PortableMessage(
        message_id=str(row["id"]),
        seq=int(row["seq"]),
        turn_id=turn_id,
        role=str(row["role"]),
        content=str(row.get("content") or ""),
        created_at=str(row.get("created_at") or ""),
        assistant_step_id=_nonempty_str(meta, "assistant_step_id"),
        tool_calls=tool_calls,
        tool_call_id=_nonempty_str(meta, "tool_call_id"),
        tool_name=(str(meta["name"]) if isinstance(meta.get("name"), str) else None),
        is_error=meta.get("is_error") is True,
        synthetic_tool_result=meta.get("synthetic_tool_result") is True,
        synthetic_reason=(
            str(meta["synthetic_reason"])
            if isinstance(meta.get("synthetic_reason"), str)
            else None
        ),
        reasoning_text=reasoning_text,
        reasoning_provenance=provenance,
        artifact_references=_attachments(
            meta,
            str(row["id"]),
            thread_id=str(row.get("thread_id") or "") or None,
            db=db,
        ),
        inbound_source=inbound_source,
        content_sha256=(
            str(meta["tool_result_sha256"])
            if row["role"] == "tool" and isinstance(meta.get("tool_result_sha256"), str)
            else None
        ),
        content_byte_count=(
            int(meta["tool_result_byte_count"])
            if row["role"] == "tool" and type(meta.get("tool_result_byte_count")) is int
            else None
        ),
        media_metadata=(
            {"media": meta["media"]}
            if row["role"] == "tool" and isinstance(meta.get("media"), dict)
            else None
        ),
        provider_message_id=(
            _nonempty_str(meta, "provider_message_id")
            if row["role"] == "assistant"
            else None
        ),
    )


def _missing_tool_result_stub(
    step_row: dict[str, Any],
    *,
    call: dict[str, Any],
) -> dict[str, Any]:
    """Project a declared call that has no durable result row in this range.

    A mid-batch process death can end a Turn before every declared call's
    result lands. The Turn is terminal, so the stub states the truth — no
    result was recorded — anchored at the Assistant Step's own durable
    position so it can never mint a synthetic boundary past a real row.
    """
    from core.turns_steps import MISSING_TOOL_RESULT_TEXT

    function = call.get("function")
    name = str(
        (function.get("name") if isinstance(function, dict) else None)
        or call.get("name")
        or "tool"
    )
    call_id = str(call["id"])
    stub = dict(step_row)
    stub["id"] = f"{step_row['id']}::{call_id}::missing"
    stub["role"] = "tool"
    stub["content"] = MISSING_TOOL_RESULT_TEXT
    stub["_meta"] = {
        "turn_id": step_row["_meta"].get("turn_id"),
        "assistant_step_id": str(step_row["id"]),
        "tool_call_id": call_id,
        "name": name,
        "is_error": True,
        "synthetic_tool_result": True,
        "synthetic_reason": "tool_result_missing",
    }
    return stub


def _quarantined_interruption_stub(
    quarantined_row: dict[str, Any],
    *,
    call_id: str,
) -> dict[str, Any]:
    """Project a declared call whose only result was quarantined unbound.

    The quarantined row's partial content never became provider truth, so the
    call projects the same synthetic interruption result a user interruption
    would leave, anchored at the quarantined row's durable position.
    """
    from core.turns_steps import USER_INTERRUPTION_TOOL_RESULT

    stub = dict(quarantined_row)
    stub["content"] = USER_INTERRUPTION_TOOL_RESULT
    meta = dict(quarantined_row["_meta"])
    meta["tool_call_id"] = call_id
    meta["synthetic_tool_result"] = True
    meta["synthetic_reason"] = "tool_result_quarantined_unbound"
    stub["_meta"] = meta
    return stub
