"""Low-level context-window admission for a rendered turn."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from core.context_payload import ContextPreflightResult
from core.token_estimation import _estimate_messages_tokens, _text_tokens
from infra.context_preview_timing import timed
from infra.queries import get_thread_project_id


DEFAULT_PREFLIGHT_MEMORY_WINDOW = 100
_CONTEXT_UNSET = object()


def fixed_prefix_section_tokens(sections: dict[str, int]) -> int:
    """Return the system-tier fixed-prefix total for a source-token ledger."""
    return sum(
        int(sections.get(section) or 0)
        for section in ("corsia", "system", "workspace")
    )


class ContextPreflightSourceResolutionError(RuntimeError):
    """Raised when protected source context cannot be inspected safely."""


@dataclass
class _PreflightUsage:
    current_tokens: int
    history_tokens: int
    source_tokens: int
    source_section_tokens: dict[str, int]
    total_tokens: int


def _thread_project_id(thread_id: str, db) -> str | None:
    try:
        return get_thread_project_id(thread_id, db=db)
    except sqlite3.OperationalError as exc:
        if "no such column: project_id" in str(exc):
            return None
        raise ContextPreflightSourceResolutionError(
            f"project scope lookup failed for {thread_id}"
        ) from exc


def _collected_context_sources(
    thread_id: str, db, *, selected_model: str | None = None,
) -> Any:
    project_id = _thread_project_id(thread_id, db)
    from core.dynamic_context_registry import collect_context_sources
    from infra.config import WORKSPACE

    try:
        return collect_context_sources(
            thread_id=thread_id,
            project_id=project_id,
            workspace=WORKSPACE,
            db=db,
            selected_model=selected_model,
        )
    except (RuntimeError, sqlite3.Error, OSError) as exc:
        detail = str(exc)
        if "project source lookup failed" in detail:
            raise ContextPreflightSourceResolutionError(detail) from exc
        raise ContextPreflightSourceResolutionError(
            f"managed context source lookup failed for {thread_id}: {detail}"
        ) from exc


def _prompt_context_sources(
    thread_id: str, db, *, selected_model: str | None = None,
    collection: Any | None = None,
) -> list[Any]:
    """Return included prompt-bearing managed sources for preflight."""
    if collection is None:
        collection = _collected_context_sources(
            thread_id, db, selected_model=selected_model,
        )
    return [
        source
        for source in collection.sources
        if getattr(source, "inclusion", None) == "included"
        and getattr(source, "prompt_content", None)
        and getattr(source, "origin_section", None)
        in {"corsia", "system", "workspace", "conductor", "project", "role", "thread", "dynamic"}
    ]


def _delivered_rows(thread_id: str, memory_window: int, db) -> list[Any]:
    return db.execute(
        "SELECT role, content, metadata FROM ("
        "SELECT rowid AS _rowid, role, content, metadata, created_at, seq FROM messages "
        "WHERE thread_id = ? AND delivery_state = 'delivered' "
        "ORDER BY seq DESC LIMIT ?"
        ") ORDER BY seq ASC",
        (thread_id, memory_window),
    ).fetchall()


def _resolve_context_window(model: str, supplied: Any) -> tuple[int, str | None]:
    from llm.capabilities import get_model_context_window
    from llm.context_window import ModelContextWindowUnknown

    if supplied is not _CONTEXT_UNSET:
        window = int(supplied or 0)
        error = None if window > 0 else f"No exact context_window evidence for model {model!r}"
        return window, error
    try:
        return int(get_model_context_window(model) or 0), None
    except ModelContextWindowUnknown as exc:
        return 0, str(exc)
    except Exception:
        return 0, f"failed to resolve context window for model {model!r}"


def _measure_base_usage(
    rows: list[Any],
    *,
    current_user_content: str,
    prepared_history: list[dict[str, Any]] | None,
    tool_definition_tokens: int,
    model: str,
) -> _PreflightUsage:
    current_tokens = _text_tokens(current_user_content, model)
    exact_tool_tokens = max(0, int(tool_definition_tokens))
    history_tokens = _estimate_messages_tokens(prepared_history, model=model) if prepared_history is not None else 0
    for row in rows:
        content_tokens = _text_tokens(row["content"] or "", model) + 4
        if prepared_history is None:
            history_tokens += content_tokens
    sections = {"tools": exact_tool_tokens} if exact_tool_tokens else {}
    return _PreflightUsage(
        current_tokens=current_tokens,
        history_tokens=history_tokens,
        source_tokens=exact_tool_tokens,
        source_section_tokens=sections,
        total_tokens=current_tokens + history_tokens + exact_tool_tokens,
    )


def _include_managed_sources(usage: _PreflightUsage, sources: list[Any], *, model: str) -> None:
    for source in sources:
        # The inventory row's own measure, so preflight and the sheet agree.
        content_tokens = source.prompt_token_count(model) or 0
        section = str(getattr(source, "origin_section", "source") or "source")
        usage.source_tokens += content_tokens
        usage.source_section_tokens[section] = (
            usage.source_section_tokens.get(section, 0) + content_tokens
        )
        usage.total_tokens += content_tokens


def _result(
    usage: _PreflightUsage,
    *,
    status: str,
    continuity_mode: str,
    model_context_window: int,
    block_reason: str | None = None,
    delivered_rows: tuple[Any, ...] = (),
) -> ContextPreflightResult:
    return ContextPreflightResult(
        status=status,
        continuity_mode=continuity_mode,
        model_context_window=model_context_window,
        total_tokens=usage.total_tokens,
        current_user_tokens=usage.current_tokens,
        history_tokens=usage.history_tokens,
        source_tokens=usage.source_tokens,
        source_section_tokens=usage.source_section_tokens,
        block_reason=block_reason,
        delivered_rows=delivered_rows,
    )


def check_context_preflight(
    *,
    thread_id: str,
    model: str,
    current_user_content: str,
    db,
    memory_window: int = DEFAULT_PREFLIGHT_MEMORY_WINDOW,
    prepared_history: list[dict[str, Any]] | None = None,
    model_context_window: int | None | object = _CONTEXT_UNSET,
    tool_definition_tokens: int = 0,
    source_collection: Any | None = None,
    timing: Any | None = None,
) -> ContextPreflightResult:
    """Return the low-level admission result for the selected model window."""

    # A caller that supplies canonical history has already selected its replay
    # boundary. Reading the archive here cannot affect the estimate and makes a
    # valid provider-resume preview needlessly reconstruct a 12k-message tail.
    rows = [] if prepared_history is not None else _delivered_rows(thread_id, memory_window, db)
    resolved_window, window_error = _resolve_context_window(model, model_context_window)
    usage = timed(
        timing, "preflight.history", _measure_base_usage,
        rows,
        current_user_content=current_user_content,
        prepared_history=prepared_history,
        tool_definition_tokens=tool_definition_tokens,
        model=model,
    )
    reason: str | None
    # ``model`` is the tokenizer's transport name, which never matches an
    # installed boundary: collecting under it re-reads every static source from
    # disk. A caller that already collected for its catalog model supplies that
    # collection instead (#2830).
    try:
        sources = _prompt_context_sources(
            thread_id, db, selected_model=model, collection=source_collection,
        )
        timed(timing, "preflight.sources", _include_managed_sources, usage, sources, model=model)
    except ContextPreflightSourceResolutionError as exc:
        reason = f"context preflight blocked: {exc}"
    else:
        reason = f"context preflight blocked: {window_error}" if window_error else None
    if reason is None and usage.total_tokens > resolved_window:
        reason = (
            "context preflight blocked: rendered payload does not fit the "
            "selected model context window and no prepared checkpoint-tail "
            f"payload was supplied ({usage.total_tokens}/{resolved_window} tokens)"
        )
    return _result(
        usage,
        status="blocked" if reason else "allowed",
        continuity_mode="blocked" if reason else "full",
        model_context_window=resolved_window,
        block_reason=reason,
        delivered_rows=tuple(rows),
    )
