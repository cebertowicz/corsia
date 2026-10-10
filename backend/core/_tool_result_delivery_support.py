"""Private read/validation mechanics for the canonical tool-result transition."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from loguru import logger

from core.tool_result_invocation import (
    invocation_id_from_echo,
    with_invocation_reference,
)
from core.tool_result_projection import render_tool_result_for_model
from tools.catalog import normalize_tool_name


_TOOL_RESULT_NAMESPACE = uuid.UUID("f2294d6f-386a-55e3-b58a-a3316da7f3d6")


class ToolResultBindingError(RuntimeError):
    """An invocation cannot be bound to one authoritative Assistant Step."""


@contextmanager
def settled_or_debt(db, *, label: str) -> Iterator[None]:
    """Run ONE unit of settlement, or leave exactly that unit as visible debt.

    THE rule this ledger now runs on: **a terminal always succeeds**. Settlement
    happens inside the terminal transaction, so a settlement that can refuse is
    a Turn that stays ``active`` with no other terminal coming — and
    ``idx_active_turns_one_active`` then forbids the thread ever opening
    another Turn. Four review passes each removed the strict read they found in
    there and each time another was found; the reads are unbounded, so the
    guarantee cannot be "every reader is total". It has to be that no reader's
    verdict reaches the terminal's outcome.

    An unpayable call is therefore not an error to propagate. It is debt, and
    debt is DECLARED, not swallowed: the projection renders a truthful
    missing-result stub for it in band. The failure is also logged here at ERROR
    with its unit and its exception.

    Scope is one unit — one call's payment, one row's quarantine — so a defect
    costs exactly the thing it defeats and the next unit still runs. The
    SAVEPOINT is what makes "exactly that unit" true: a half-written unit is
    rolled back rather than committed alongside the terminal.

    A savepoint that cannot be unwound is the one case that still raises.
    SQLite rolls the whole transaction back on ``SQLITE_FULL``, ``SQLITE_IOERR``
    and friends, which takes the terminal's own CAS with it; there is nothing
    left to commit and a retry is exactly what such a failure needs (rule: only
    DEBT PAYMENT degrades — the terminal's own I/O still fails).

    The name is unique per unit because these nest — a Turn's terminal guards
    its stranded-row sweep, which guards each row inside it — and ``ROLLBACK
    TO`` on a shared name targets the most recent savepoint holding it, which
    is a correctness question nobody should have to re-derive at a call site.
    """
    savepoint = f"settled_or_debt_{uuid.uuid4().hex}"
    db.execute(f"SAVEPOINT {savepoint}")
    try:
        yield
    except Exception as exc:  # noqa: BLE001 - degrade to debt, never to silence
        try:
            db.execute(f"ROLLBACK TO {savepoint}")
            db.execute(f"RELEASE {savepoint}")
        except Exception:
            logger.error(
                "settlement of {} lost its savepoint ({}: {}); the transaction "
                "is gone, so the terminal itself fails and can be retried",
                label, type(exc).__name__, exc,
            )
            raise exc from None
        logger.error(
            "settlement of {} failed ({}: {}); it stays unpaid debt, named by "
            "violations() and projected as a missing-result stub",
            label, type(exc).__name__, exc,
        )
    else:
        db.execute(f"RELEASE {savepoint}")


@dataclass(frozen=True)
class ToolExecutionResult:
    """One canonical tool-result message and its durable lineage."""

    message_id: str
    assistant_step_id: str
    invocation_id: str
    provider_tool_call_id: str | None
    name: str
    content: str
    content_sha256: str
    content_byte_count: int
    is_error: bool
    # Immutable reference describing media this result read. Never bytes: the
    # provider boundary resolves them from the referenced path on demand.
    media_metadata: dict[str, Any] | None = None
    # Why CORSIA authored this result instead of a tool, for a synthetic row.
    synthetic_reason: str | None = None

    @property
    def role(self) -> str:
        return "tool"

    def render_for_model(
        self,
        *,
        thread_id: str,
        budget_tokens: int | None,
        include_invocation_reference: bool = False,
    ) -> str:
        projected = render_tool_result_for_model(
            thread_id,
            self,
            budget_tokens=budget_tokens,
        )
        if not include_invocation_reference:
            return projected
        return with_invocation_reference(projected, self.invocation_id)



def required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolResultBindingError(f"{label} is required")
    return value.strip()


def metadata_object(raw: object) -> dict[str, Any]:
    """Read metadata that a binding decision is about to be made FROM.

    DELIBERATELY STRICT, decided in the pass that made the terminal
    unfailable. Every caller here is asking "is this row already the result I
    am about to write?" or "which call does this row answer?", and an
    unreadable row answers neither. Degrading to ``{}`` would make a corrupt
    row look like a row bound to nothing — which
    :func:`require_same_result` would then read as "a DIFFERENT result is
    bound here", or the stranded sweep as "no owner", both of which act on a
    fact nobody established.

    Its strictness no longer reaches a terminal's outcome: both in-terminal
    callers (payment through ``existing_result``, and the stranded-row sweep)
    run inside :func:`settled_or_debt`, so an unreadable row costs its own unit
    and stays named debt.
    """
    if isinstance(raw, dict):
        return dict(raw)
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError) as exc:
        raise ToolResultBindingError("tool-result metadata is malformed") from exc
    if not isinstance(parsed, dict):
        raise ToolResultBindingError("tool-result metadata is not an object")
    return parsed


def declared_calls(raw: object) -> list[dict[str, Any]]:
    metadata = metadata_object(raw)
    calls = metadata.get("llm_tool_calls") or metadata.get("tool_calls") or []
    return [call for call in calls if isinstance(call, dict)]


def call_parts(call: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    function = call.get("function")
    if not isinstance(function, dict):
        function = {}
    call_id = str(call.get("id") or "")
    name = str(function.get("name") or call.get("name") or "")
    raw_arguments = function.get("arguments", call.get("arguments") or {})
    if isinstance(raw_arguments, str):
        try:
            raw_arguments = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError as exc:
            raise ToolResultBindingError(
                f"Assistant Step tool call {call_id!r} has malformed arguments"
            ) from exc
    arguments = raw_arguments if isinstance(raw_arguments, dict) else {}
    return call_id, name, arguments


def message_id(
    thread_id: str,
    assistant_step_id: str,
    invocation_id: str,
) -> str:
    """Return the one canonical row identity for this tool invocation."""
    return str(
        uuid.uuid5(
            _TOOL_RESULT_NAMESPACE,
            f"{thread_id}\0{assistant_step_id}\0{invocation_id}",
        )
    )


def stamped_byte_count(value: object) -> int:
    """Read a stamped byte count without ever refusing to read the row.

    A bare ``int()`` raises on a corrupted stamp, and this reader runs inside a
    Turn's terminal transaction (through ``existing_result`` and the stranded
    sweep), where a raise is not a skipped row but a Turn left active with no
    other terminal coming — the same "a reader's strictness wedges a writer"
    shape this ledger exists to remove. Unreadable reads as 0, which is exactly
    what an ABSENT stamp already reads as here, so nothing starts trusting a
    corrupted row: ``core.tool_result_projection`` compares the stamp against
    the actual bytes and still rejects the mismatch.
    """
    if not isinstance(value, (int, float, str)):
        return 0
    try:
        return int(value)
    except (ValueError, OverflowError):
        return 0


def result_from_row(row: Any) -> ToolExecutionResult:
    metadata = metadata_object(row["metadata"])
    content = str(row["content"] or "")
    return ToolExecutionResult(
        message_id=str(row["id"]),
        assistant_step_id=str(metadata.get("assistant_step_id") or ""),
        invocation_id=str(metadata.get("tool_invocation_id") or ""),
        provider_tool_call_id=(
            str(metadata["tool_call_id"])
            if metadata.get("tool_call_id")
            else None
        ),
        name=str(metadata.get("name") or ""),
        content=content,
        content_sha256=str(metadata.get("tool_result_sha256") or ""),
        content_byte_count=stamped_byte_count(
            metadata.get("tool_result_byte_count")
        ),
        is_error=bool(metadata.get("is_error")),
        media_metadata=(
            {"media": metadata["media"]}
            if isinstance(metadata.get("media"), dict)
            else None
        ),
        synthetic_reason=(
            str(metadata["synthetic_reason"])
            if metadata.get("synthetic_reason")
            else None
        ),
    )


def existing_result(db, exact_message_id: str) -> ToolExecutionResult | None:
    row = db.execute(
        "SELECT id,role,content,metadata FROM messages WHERE id=?",
        (exact_message_id,),
    ).fetchone()
    if row is None:
        return None
    if row["role"] != "tool":
        raise ToolResultBindingError(
            "canonical tool-result identity collides with a non-tool message"
        )
    return result_from_row(row)


def require_same_result(
    existing: ToolExecutionResult,
    *,
    thread_id: str,
    turn_id: str,
    assistant_step_id: str,
    invocation_id: str,
    provider_tool_call_id: str | None,
    name: str,
    arguments: dict[str, Any],
    content: str,
    is_error: bool,
    db,
) -> ToolExecutionResult:
    row = db.execute(
        "SELECT thread_id,metadata FROM messages WHERE id=?",
        (existing.message_id,),
    ).fetchone()
    metadata = metadata_object(row["metadata"]) if row is not None else {}
    same = (
        row is not None
        and row["thread_id"] == thread_id
        and metadata.get("turn_id") == turn_id
        and existing.assistant_step_id == assistant_step_id
        and existing.invocation_id == invocation_id
        and (
            provider_tool_call_id is None
            or existing.provider_tool_call_id == provider_tool_call_id
        )
        and normalize_tool_name(existing.name) == normalize_tool_name(name)
        and metadata.get("arguments") == arguments
        and existing.content == content
        and existing.is_error is is_error
    )
    if not same:
        raise ToolResultBindingError(
            "tool invocation identity is already bound to a different result"
        )
    return existing


def select_provider_call_from_metadata(
    *,
    metadata: object,
    assistant_step_id: str,
    provider_tool_call_id: str | None,
    name: str,
    arguments: dict[str, Any] | None,
) -> tuple[str | None, dict[str, Any]]:
    """Validate one call against an authoritative Assistant Step projection."""
    candidates: list[tuple[str, dict[str, Any]]] = []
    for call in declared_calls(metadata):
        call_id, call_name, call_arguments = call_parts(call)
        if provider_tool_call_id is not None and call_id != provider_tool_call_id:
            continue
        if normalize_tool_name(call_name) != normalize_tool_name(name):
            continue
        if arguments is not None and call_arguments != arguments:
            continue
        candidates.append((call_id, call_arguments))
    if provider_tool_call_id is not None:
        if len(candidates) == 1 and candidates[0][0] == provider_tool_call_id:
            return candidates[0]
        raise ToolResultBindingError(
            f"Assistant Step {assistant_step_id!r} does not declare provider "
            f"tool call {provider_tool_call_id!r}"
        )
    if not candidates:
        raise ToolResultBindingError(
            f"Assistant Step {assistant_step_id!r} has no matching provider call"
        )
    if arguments is not None:
        return None, dict(arguments)
    if len(candidates) != 1:
        raise ToolResultBindingError(
            "provisional tool result needs arguments to identify its declared shape"
        )
    return None, candidates[0][1]


def select_provider_call(
    *,
    db,
    thread_id: str,
    assistant_step_id: str,
    provider_tool_call_id: str | None,
    name: str,
    arguments: dict[str, Any] | None,
) -> tuple[str | None, dict[str, Any]]:
    row = db.execute(
        "SELECT metadata FROM messages "
        "WHERE id=? AND thread_id=? AND role='assistant'",
        (assistant_step_id, thread_id),
    ).fetchone()
    if row is None:
        raise ToolResultBindingError(
            f"missing authoritative Assistant Step {assistant_step_id!r} predecessor"
        )
    return select_provider_call_from_metadata(
        metadata=row["metadata"],
        assistant_step_id=assistant_step_id,
        provider_tool_call_id=provider_tool_call_id,
        name=name,
        arguments=arguments,
    )


def validate_execution_request(
    *,
    thread_id: str,
    turn_id: str,
    invocation_id: str,
    assistant_step_id: str | None,
    name: str,
    arguments: dict[str, Any] | None,
    content: str,
) -> tuple[str, str, str, str, str]:
    values = (
        required_text(thread_id, "thread identity"),
        required_text(turn_id, "turn identity"),
        required_text(invocation_id, "tool invocation identity"),
        required_text(assistant_step_id, "Assistant Step predecessor"),
        required_text(name, "tool name"),
    )
    if arguments is not None and not isinstance(arguments, dict):
        raise ToolResultBindingError("tool arguments must be an object")
    if not isinstance(content, str):
        raise ToolResultBindingError("tool result content must be text")
    return values
