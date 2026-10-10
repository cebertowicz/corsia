"""Pre-dispatch projection used by the CTX widget for the next turn."""

from __future__ import annotations

from dataclasses import replace
from functools import partial
from typing import Any

from core import dynamic_context_registry
from core.context_admission import fixed_prefix_section_tokens
from core.context_preview_projection import PreviewInputs, preview_usage_for_next_turn
from infra.context_preview_timing import timed as _timed
from infra.context_receipts import positive_window
from infra.config import WORKSPACE
from infra.execution_class import execution_class
from infra.send_dispatch import provider_for
from llm.resolution import resolve_execution_target


def collect_context_sources(**kwargs):
    """Resolve the canonical registry at call time, not module-import time."""
    return dynamic_context_registry.collect_context_sources(**kwargs)


def _field(row: Any, key: str) -> Any:
    if hasattr(row, "keys") and key in row.keys():
        return row[key]
    if isinstance(row, dict):
        return row.get(key)
    return None


def _message_count(thread_id: str, db) -> int:
    row = db.execute(
        "SELECT COUNT(*) AS count FROM messages "
        "WHERE thread_id = ? AND delivery_state = 'delivered'",
        (thread_id,),
    ).fetchone()
    if not row:
        return 0
    return int(row["count"] if "count" in row.keys() else row[0])


def _history_message_count(preflight: dict[str, Any], thread_id: str, db) -> int:
    estimated = preflight.get("history_messages")
    return _message_count(thread_id, db) if estimated is None else int(estimated)


def _checkpoint_eligibility_status(
    *, thread_id: str, thread_row: Any, db, rendered_projection: Any | None = None,
) -> tuple[bool, str | None]:
    """Same admission rule the checkpoint-start endpoint applies (#1952).

    The sheet must never offer Checkpoint Now for a click that would be
    refused, so this reuses the one eligibility rule instead of a second,
    driftable copy of it.
    """
    from core.checkpoint_eligibility import checkpoint_eligibility
    from history.types import PortableHistoryError

    user_id = _field(thread_row, "user_id")
    if not user_id:
        return False, "thread_not_found"
    try:
        eligibility = checkpoint_eligibility(
            thread_id, user_id, db=db, thread_row=thread_row,
            rendered_projection=rendered_projection,
        )
    except PortableHistoryError:
        # A history that cannot be projected cannot be checkpointed either;
        # the widget must still render, just without the shortcut.
        return False, "history_unprojectable"
    if eligibility is None:
        return False, "thread_not_found"
    return eligibility.eligible, eligibility.reason_key


def _with_model_override(thread_row: Any, model_override: str | None) -> Any:
    """Copy the thread row with the previewed model selected, if one is given."""
    if not model_override:
        return thread_row
    if hasattr(thread_row, "keys"):
        thread_row = {key: thread_row[key] for key in thread_row.keys()}
    elif not isinstance(thread_row, dict):
        thread_row = dict(thread_row)
    thread_row["model"] = model_override
    return thread_row


def _discovered_window(provider: str, target: Any) -> int | None:
    """Name the provider window beside the counted one, as receipts do (#1753).

    Only an external harness can run past a CORSIA budget; Native builds its
    prompt against the counted window, so it reports none.
    """
    if provider not in {"cc", "codex"}:
        return None
    return positive_window(target.discovered_context_window, absent=None)


def _refresh_preview_limit(prepared, window):
    """Re-evaluate the same frozen input under current policy, without re-rendering."""
    from core.destination_preflight import attach_destination_fit_ledger

    ledger = prepared.fit_ledger
    if ledger is None or ledger.effective_window_tokens == window:
        return prepared
    refreshed = attach_destination_fit_ledger(
        replace(prepared, fit_ledger=None), harness=ledger.harness,
        proven_target_window_tokens=window,
    )
    return replace(refreshed, fit_ledger=replace(
        refreshed.fit_ledger, input_count_provenance=ledger.input_count_provenance,
    ))


def _provider_resume_decision(
    thread_id: str, *, provider: str, thread_row: Any, target: Any,
    runtime_truth: dict | None, db,
):
    """Return the single resume decision shared by the preview paths.

    Pass the owning thread row: resume needs the stored model to be the
    selected one, as on send, so a previewed override is a switch and keeps
    the selected model's estimate.
    """
    from infra.provider_resume import provider_resume_decision

    return provider_resume_decision(
        thread_id,
        provider=provider,
        thread_row=thread_row,
        target=target,
        runtime_truth=runtime_truth or {},
        db=db,
    )


def build_next_turn_context_preview(
    thread_id: str,
    thread_row: Any,
    *,
    db,
    current_user_content: str = "",
    model_override: str | None = None,
    tool_definitions: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
    expected_provider: str | None = None,
    prepared_context: Any | None = None,
    runtime_truth: dict | None = None,
    timing=None,
) -> dict[str, Any]:
    """Return the CTX projection for the selected next-turn destination."""
    owning_thread_row = thread_row
    thread_row = _with_model_override(thread_row, model_override)
    model = _field(thread_row, "model") or ""
    target = resolve_execution_target(model, db=db)
    klass = execution_class(thread_row, target=target)
    provider, is_subagent = provider_for(klass)
    if expected_provider is not None and provider != expected_provider:
        raise RuntimeError(
            "next-turn preview provider changed during tool-ledger selection "
            f"({expected_provider!r} -> {provider!r})"
        )
    transport_model = target.transport_model
    project_id = _field(thread_row, "project_id")

    from core.destination_preflight import result_for_prepared_context
    from core.token_estimation import _text_tokens
    from core.native_context_prefix import (
        _canonical_tool_definitions,
        _selected_tool_definitions,
    )

    frozen_tool_definitions = _selected_tool_definitions(tool_definitions or ())
    canonical_tool_definitions = _canonical_tool_definitions(
        frozen_tool_definitions
    )
    inventory_tool_tokens = (
        _text_tokens(canonical_tool_definitions, transport_model) + 4
        if canonical_tool_definitions
        else 0
    )
    resume_decision = _timed(
        timing, "provider_resume", _provider_resume_decision, thread_id,
        provider=provider, thread_row=owning_thread_row, target=target,
        runtime_truth=runtime_truth, db=db,
    )

    # One collection serves the estimate and the projected source rows (#2830).
    collection = _timed(
        timing, "source_collection", collect_context_sources, thread_id=thread_id,
        project_id=project_id, workspace=WORKSPACE, db=db, selected_model=target.catalog_id,
    )
    if prepared_context is None:
        from core.context_preview_estimate import estimated_preflight

        preflight = _timed(
            timing, "preflight", partial(estimated_preflight, timing=timing), thread_id,
            model=transport_model or model, source_collection=collection,
            current_user_content=current_user_content, db=db,
            live_window=int(target.context_window or 0),
            tool_definition_tokens=inventory_tool_tokens,
            retained_provider_tokens=resume_decision.retained_tokens,
        )
    else:
        if prepared_context.target_catalog_id != target.catalog_id:
            raise RuntimeError(
                "next-turn preview prepared target changed before projection"
            )
        result = _timed(timing, "preflight", result_for_prepared_context, _refresh_preview_limit(prepared_context, target.context_window))
        preflight = result.to_dict()
        preflight.pop("prepared_context", None)
    rendered_projection = preflight.pop("_checkpoint_eligibility_projection", None)
    section_tokens = preflight["source_section_tokens"]
    history_messages = _history_message_count(preflight, thread_id, db)
    system_tokens = fixed_prefix_section_tokens(section_tokens) + int(
        preflight.get("checkpoint_tokens") or 0
    )
    tool_def_tokens = inventory_tool_tokens
    history_tokens = int(preflight.get("history_tokens") or 0)
    user_tokens = int(preflight["current_user_tokens"])
    checkpoint_eligible, checkpoint_ineligible_reason = _timed(
        timing, "eligibility", _checkpoint_eligibility_status, thread_id=thread_id,
        thread_row=owning_thread_row, db=db,
        rendered_projection=rendered_projection,
    )
    return _timed(timing, "preview_projection", preview_usage_for_next_turn, PreviewInputs(
        thread_id=thread_id,
        provider=provider,
        model=model,
        klass=klass,
        is_subagent=is_subagent,
        project_id=project_id,
        collection=collection,
        current_user_content=current_user_content,
        preflight=preflight,
        frozen_tool_definitions=frozen_tool_definitions,
        canonical_tool_definitions=canonical_tool_definitions,
        history_messages=history_messages,
        system_tokens=system_tokens,
        tool_def_tokens=tool_def_tokens,
        history_tokens=history_tokens,
        user_tokens=user_tokens,
        checkpoint_eligible=checkpoint_eligible,
        checkpoint_ineligible_reason=checkpoint_ineligible_reason,
        discovered_context_window=_discovered_window(provider, target),
    ))
