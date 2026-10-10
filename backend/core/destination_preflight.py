"""Canonical destination-fit accounting for provider payloads."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from core.context_payload import (
    ContextPreflightResult,
    PreparedContextPayload,
    RenderedDestinationPayload,
)
from core.destination_fit import (
    build_destination_fit_ledger,
    partition_destination_sections,
)
from core.token_estimation import _estimate_messages_tokens, _text_tokens


def attach_destination_fit_ledger(
    prepared: PreparedContextPayload,
    *,
    harness: str,
    proven_target_window_tokens: int | None,
    model: str | None = None,
) -> PreparedContextPayload:
    """Attach the sole fit decision to one normalized frozen provider payload."""
    if prepared.fit_ledger is not None:
        raise ValueError("prepared destination already has a canonical fit ledger")
    rendered = prepared.rendered
    if (
        rendered.accounting_payload is None
        or rendered.fit_section_tokens is None
        or rendered.fit_components is None
        or not rendered.fit_input_identity
    ):
        raise ValueError(
            f"{harness!r} renderer omitted canonical destination-fit inputs"
        )
    retained = rendered.fit_retained_provider_tokens
    if isinstance(retained, bool) or not isinstance(retained, int) or retained < 0:
        raise ValueError("retained provider tokens must be a non-negative integer")
    sections = dict(rendered.fit_section_tokens)
    sections["fixed"] = int(sections.get("fixed") or 0) + retained
    from core.token_estimation import request_count_identity

    ledger = build_destination_fit_ledger(
        harness=harness,
        target_catalog_id=prepared.target_catalog_id,
        proven_target_window_tokens=proven_target_window_tokens,
        accounting_payload=rendered.accounting_payload,
        rendered_input_tokens=int(rendered.input_tokens) + retained,
        section_tokens=sections,
        input_identity=rendered.fit_input_identity,
        current_input=rendered.fit_current_input,
        components=rendered.fit_components,
        input_count_provenance=request_count_identity(model),
    )
    return replace(
        prepared,
        model_context_window=ledger.effective_window_tokens,
        fit_ledger=ledger,
    )


def attach_native_provider_fit_ledger(
    *,
    target_catalog_id: str,
    proven_target_window_tokens: int,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    input_identity: str,
    current_input: Any,
    model: str | None = None,
) -> PreparedContextPayload:
    """Attach canonical fit proof for one exact Native messages/tools payload."""
    message_tokens = _estimate_messages_tokens(messages, model=model)
    canonical_tools = (
        json.dumps(
            tuple(tools),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        if tools
        else ""
    )
    tool_tokens = _text_tokens(canonical_tools, model) + 4 if canonical_tools else 0
    rendered_tokens = message_tokens + tool_tokens
    current_tokens = (
        _estimate_messages_tokens([current_input], model=model)
        if isinstance(current_input, dict)
        else _estimate_messages_tokens([{"role": "user", "content": current_input}], model=model) - 4
    )
    sections = partition_destination_sections(
        rendered_tokens,
        fixed=rendered_tokens,
        checkpoint=0,
        tail=0,
        current_input=current_tokens,
        tools=tool_tokens,
    )
    payload = {"messages": messages, "tools": tools}
    prepared = PreparedContextPayload(
        mode="exact_replay",
        target_catalog_id=target_catalog_id,
        model_context_window=proven_target_window_tokens,
        history_epoch=0,
        sealed_boundary=None,
        replay=None,
        rendered=RenderedDestinationPayload(
            payload=payload,
            input_tokens=rendered_tokens,
            accounting_payload=payload,
            fit_section_tokens=sections,
            fit_input_identity=input_identity,
            fit_current_input=current_input,
            fit_components={
                "fixed": messages,
                "checkpoint": "",
                "tail": [],
                "current_input": current_input,
                "tools": tools,
            },
        ),
    )
    return attach_destination_fit_ledger(
        prepared,
        harness="native",
        model=model,
        proven_target_window_tokens=proven_target_window_tokens,
    )


def result_for_prepared_context(
    prepared: PreparedContextPayload,
    *,
    trigger_reason: str | None = None,
) -> ContextPreflightResult:
    """Build the public preflight result from a frozen destination payload."""
    ledger = prepared.fit_ledger
    if ledger is None:
        raise ValueError(
            "prepared destination has no canonical renderer-owned fit ledger"
        )
    window = ledger.effective_window_tokens
    current_user_tokens = max(0, int(prepared.rendered.current_user_tokens))
    total = ledger.rendered_input_tokens
    continuity_mode = prepared.mode
    if not ledger.fits:
        return ContextPreflightResult(
            status="blocked",
            continuity_mode="blocked",
            model_context_window=window,
            total_tokens=total,
            current_user_tokens=current_user_tokens,
            source_tokens=prepared.rendered.source_tokens,
            source_section_tokens=prepared.rendered.source_section_tokens or {},
            block_reason=ledger.block_reason,
            trigger_reason=trigger_reason,
            prepared_context=prepared,
        )
    return ContextPreflightResult(
        status="allowed",
        continuity_mode=continuity_mode,
        model_context_window=window,
        total_tokens=total,
        current_user_tokens=current_user_tokens,
        source_tokens=prepared.rendered.source_tokens,
        source_section_tokens=prepared.rendered.source_section_tokens or {},
        trigger_reason=trigger_reason,
        prepared_context=prepared,
    )
