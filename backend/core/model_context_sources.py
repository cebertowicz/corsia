"""Model/harness effective-context source helpers."""

from __future__ import annotations

from typing import Any

from core.effective_context import ContextSourceEntry


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _label_for_harness(harness: str) -> str:
    if harness == "claude-code":
        return "Claude Code"
    if harness == "codex":
        return "Codex"
    if harness == "native":
        return "Native"
    return harness.replace("-", " ").title() or "Model"


def model_context_source_entry(facts: dict[str, Any]) -> ContextSourceEntry:
    """Return the inspectable CTX source row for selected harness/model facts."""
    harness = _text(facts.get("harness")) or "unknown"
    model = _text(facts.get("model")) or _text(facts.get("model_alias"))
    included = bool(model)
    return ContextSourceEntry(
        source_id=f"model:{harness}",
        origin_section="model",
        label=(
            f"{_label_for_harness(harness)} model: {model}"
            if model
            else f"{_label_for_harness(harness)} model"
        ),
        stable_reference=f"model://{model}" if model else f"harness://{harness}",
        freshness="fresh" if included else "missing",
        inclusion="included" if included else "omitted",
        reason="selected harness model" if included else "selected harness has no model",
        owner_surface="provider-settings",
        metadata={
            key: value
            for key, value in facts.items()
            if value is not None
        },
    )


def agent_send_envelope_source_entry(
    *,
    thread_id: str,
    harness: str,
    agent_envelope: Any | None,
    model: str | None = None,
) -> ContextSourceEntry | None:
    """Attribute CORSIA-owned agent framing without charging it to the user."""
    managed_content = getattr(agent_envelope, "managed_content", "")
    if not managed_content:
        return None

    from core.token_estimation import _text_tokens

    managed_tokens = max(
        0,
        _text_tokens(agent_envelope.content, model)
        - _text_tokens(agent_envelope.body_content, model),
    )
    return ContextSourceEntry(
        source_id=f"{harness}:agent-send-envelope",
        origin_section="provider",
        label="Agent-send envelope",
        stable_reference=f"thread://{thread_id}/agent-send-envelope",
        freshness="fresh",
        inclusion="included",
        reason="exact CORSIA-managed agent-send framing",
        owner_surface="message-format",
        prompt_content=managed_content,
        prompt_tokens_override=managed_tokens,
        metadata={
            "provider": "agent_send_envelope",
            "has_reply_directive": bool(agent_envelope.managed_suffix),
        },
    )
