"""Pre-dispatch CTX estimate for threads with no frozen provider payload."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any


@dataclass(frozen=True, slots=True)
class InstalledCheckpointHistory:
    """Checkpoint tail material and its exact eligibility boundary."""

    history: list[dict[str, Any]]
    component: str
    eligibility_projection: Any


def _installed_checkpoint_history(
    thread_id: str,
    db,
    timing: Any | None = None,
) -> InstalledCheckpointHistory | None:
    """Render the installed checkpoint tail and its system-prompt component.

    The checkpoint is a system-prompt component, as the Native prefix appends
    it (native_context_sources.checkpoint_material), never a history message
    (#1388).
    """
    from core.checkpoint_eligibility import CheckpointEligibilityProjection
    from core.checkpoint_resume import installed_replay_with_exact_seal
    from core.native_context_sources import checkpoint_material
    from core.tool_result_projection import HARNESS_TOOL_RESULT_BUDGET_TOKENS
    from history.adapters import to_native_history
    from infra.checkpoint_queries import current_installed
    from infra.context_preview_timing import timed

    prefix = "preflight.checkpoint_tail"
    timed(timing, f"{prefix}.interpreter_yield", time.sleep, 0)
    installed = timed(
        timing, f"{prefix}.current_installed", current_installed, thread_id, db=db,
    )
    if installed is None:
        return None
    selection, exact_seal = installed_replay_with_exact_seal(
        installed, db=db, timing=timing, timing_prefix=prefix,
    )
    history = timed(
        timing,
        f"{prefix}.to_native_history",
        to_native_history,
        selection.tail,
        tool_result_budget_tokens=HARNESS_TOOL_RESULT_BUDGET_TOKENS,
        tool_argument_budget_tokens=HARNESS_TOOL_RESULT_BUDGET_TOKENS,
    )
    _prompt, component = checkpoint_material(selection)
    return InstalledCheckpointHistory(
        history=history,
        component=component,
        eligibility_projection=CheckpointEligibilityProjection(
            start_exclusive=selection.checkpoint_boundary,
            sealed_boundary=exact_seal,
        ),
    )


def _looked_up_tools(
    thread_id: str, db, prepared_history: list[dict[str, Any]] | None,
    retained_provider_tokens: int | None, model: str,
) -> list[dict[str, Any]]:
    """Attribute looked-up tool definitions from the history the estimate measured.

    That is the checkpoint-tail message list, or the stored rows when no
    checkpoint is installed. A provider-resume handle holds its history in the
    harness and reports none.
    """
    from core.context_admission import DEFAULT_PREFLIGHT_MEMORY_WINDOW, _delivered_rows
    from core.loaded_tool_usage import loaded_tool_tokens
    from tools.mcp_runtime import tool_server_index

    if retained_provider_tokens is not None:
        return []
    rows = None if prepared_history is not None else _delivered_rows(
        thread_id, DEFAULT_PREFLIGHT_MEMORY_WINDOW, db,
    )
    return loaded_tool_tokens(prepared_history, tool_server_index(), rows=rows, model=model)


def _count_retained_once(estimate: dict[str, Any], retained_provider_tokens: int) -> None:
    """Total a provider-resume preview from the retained count, once.

    The retained count is the provider's whole input (prefix, tools and
    history, plus the terminal tail), so the fixed sources it already holds are
    not added again, and History is only what remains after them. Fixed
    estimates above the retained count leave History at zero and the aggregate
    truthful, never rescaled.
    """
    retained = max(0, int(retained_provider_tokens))
    estimate["history_tokens"] = max(0, retained - estimate["source_tokens"])
    estimate["total_tokens"] = retained + estimate["current_user_tokens"]


def estimated_preflight(
    thread_id: str,
    *,
    model: str,
    current_user_content: str,
    db,
    live_window: int,
    tool_definition_tokens: int,
    retained_provider_tokens: int | None = None,
    source_collection: Any | None = None,
    timing: Any | None = None,
) -> dict[str, Any]:
    """Estimate widget occupancy before any frozen provider payload exists.

    Dispatch admission stays "unknown" — only a frozen provider-rendered
    payload can produce a verdict — but the numbers are composed from the
    single live window resolution plus the actual context material so a fresh
    thread shows a real estimate instead of 0%.
    """
    from core.context_admission import check_context_preflight
    from core.token_estimation import _text_tokens
    from infra.context_preview_timing import timed

    # A trusted live provider handle already owns its compacted history. Reusing
    # its measured capacity avoids reconstructing transcript rows the harness no
    # longer carries; cold previews retain the checkpoint/archive path below.
    installed = None if retained_provider_tokens is not None else timed(
        timing, "preflight.checkpoint_tail", _installed_checkpoint_history, thread_id, db, timing,
    )
    prepared_history, checkpoint_component = (installed.history, installed.component) if installed else (None, "")
    if retained_provider_tokens is not None:
        prepared_history = []
    estimate = check_context_preflight(
        thread_id=thread_id,
        model=model,
        current_user_content=current_user_content,
        db=db,
        model_context_window=live_window,
        tool_definition_tokens=tool_definition_tokens,
        prepared_history=prepared_history,
        source_collection=source_collection,
        timing=timing,
    ).to_dict()
    estimate.pop("prepared_context", None)
    loaded = _looked_up_tools(thread_id, db, prepared_history, retained_provider_tokens, model)
    if retained_provider_tokens is not None:
        _count_retained_once(estimate, retained_provider_tokens)
        checkpoint_tokens = 0
    else:
        checkpoint_tokens = _text_tokens(checkpoint_component, model)
        estimate["total_tokens"] += checkpoint_tokens
        estimate["source_tokens"] += checkpoint_tokens
    continuity_mode = (
        "provider_resume" if retained_provider_tokens is not None
        else "checkpoint_tail" if prepared_history is not None else "unknown"
    )
    return {
        **estimate,
        "status": "unknown",
        "continuity_mode": continuity_mode,
        "history_messages": (
            0 if retained_provider_tokens is not None
            else len(prepared_history) if prepared_history is not None else None
        ),
        "checkpoint_tokens": checkpoint_tokens,
        "loaded_tool_tokens": loaded,
        "model_context_window": live_window,
        "block_reason": None,
        "trigger_reason": None,
        "admission_unknown_reason": (
            "dispatch admission requires a frozen provider-rendered payload"
        ),
        "destination_fit_ledger": None,
        "_checkpoint_eligibility_projection": installed.eligibility_projection if installed else None,
    }
