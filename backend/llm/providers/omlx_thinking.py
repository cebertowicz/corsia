"""oMLX inference request shaping — CORSIA effort levels → oMLX wire fields.

Split out of ``llm.providers.omlx``. Re-exported by ``omlx.py`` for callers.

Effort is sent as ``reasoning_effort``, the field each model's chat template
actually reads. It rides inside ``extra_body`` rather than as a top-level
litellm parameter: litellm's allowlist rejects the top-level form for a generic
OpenAI-compatible model, while extra_body is merged into the request JSON, so
the server sees the same field either way. It used to be converted into a
``thinking_budget`` token
ceiling, which is a different mechanism: a budget truncates thinking at a token
count, it does not ask the model to reason harder. Driving a depth control from
a truncation cap meant the selector could not change reasoning depth at all.

CORSIA no longer sends ``thinking_budget``. oMLX applies its own per-model
``thinking_budget_tokens`` when a request omits the field, so the ceiling stays
where it belongs — configured on the server, not welded to a user control.

The level is checked against the vocabulary the server reported for the model
before it goes on the wire. oMLX would otherwise remap an unknown level to the
model's default without telling anyone, so a stale persisted value or an API
caller could ask for one level while the model ran at another.
"""

from __future__ import annotations

from typing import Any


class UnsupportedReasoningEffortError(ValueError):
    """The requested level is not one the model's chat template accepts."""

    def __init__(self, effort: str, values: list[str]) -> None:
        self.effort = effort
        self.values = list(values)
        options = ", ".join(self.values)
        super().__init__(
            f"Reasoning level '{effort}' is not one this model accepts "
            f"({options}). Pick a level in the thinking control."
        )


def wire_reasoning_effort(
    reasoning_effort: str | None,
    reasoning_config: dict[str, Any] | None = None,
) -> str | None:
    """Return the effort to put on the wire, or None to omit the field.

    ``auto`` and ``none`` both omit it: ``auto`` defers to the server's own
    setting, and ``none`` is expressed by ``enable_thinking=False`` instead.

    ``reasoning_config`` is the shape the server reported for this model:

    * ``effort_levels``: the level must be one of ``values``, otherwise
      :class:`UnsupportedReasoningEffortError` is raised rather than letting
      oMLX quietly substitute the model's default;
    * ``boolean``: the model has no discrete levels, so no level is sent and
      thinking stays on at the server's setting;
    * None: the server did not report a vocabulary (too old, or a free-form
      template), so the value passes through and the server validates it.
    """
    if not reasoning_effort:
        return None
    effort = reasoning_effort.strip().lower()
    if effort in ("auto", "none", ""):
        return None
    if reasoning_config is None:
        return effort
    if reasoning_config.get("type") == "boolean":
        return None
    if reasoning_config.get("type") == "effort_levels":
        values = [str(v) for v in reasoning_config.get("values") or []]
        if effort not in values:
            raise UnsupportedReasoningEffortError(effort, values)
    return effort


def make_inference_extra_body(
    reasoning_effort: str | None,
    preserve_thinking: bool | None = None,
    reasoning_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build oMLX ``extra_body`` fields for the requested reasoning effort.

    Carries the thinking on/off toggle and preserve-thinking; the level itself
    is added by ``make_prompt_extra_body``. For a toggle-only model (``boolean``
    shape) ON asserts ``enable_thinking=True`` explicitly: deferring to the
    server would show ON while a server configured for thinking-off did not
    think.
    """
    out: dict[str, Any] = {}
    if isinstance(preserve_thinking, bool):
        out["chat_template_kwargs"] = {
            "preserve_thinking": preserve_thinking,
        }
    if not reasoning_effort:
        return out
    effort = reasoning_effort.strip().lower()
    if effort == "none":
        out.setdefault("chat_template_kwargs", {})["enable_thinking"] = False
    elif (
        effort != "auto"
        and reasoning_config is not None
        and reasoning_config.get("type") == "boolean"
    ):
        out.setdefault("chat_template_kwargs", {})["enable_thinking"] = True
    return out


def make_prompt_extra_body(
    messages: list[dict[str, Any]],
    reasoning_effort: str | None = None,
    preserve_thinking: bool | None = None,
    reasoning_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the oMLX fields that shape this message prefix.

    Includes ``reasoning_effort`` because the chat template injects it into the
    prompt: anything comparing or reproducing a prefix (cache warm, probe) must
    render with the same level or it measures a prefix the turn never sends.
    Raises :class:`UnsupportedReasoningEffortError` for a level outside the
    model's reported vocabulary.
    """
    has_prior_reasoning = any(
        message.get("role") == "assistant"
        and isinstance(message.get("reasoning_content"), str)
        and bool(message["reasoning_content"])
        for message in messages
    )
    shape = make_inference_extra_body(
        reasoning_effort,
        preserve_thinking=(preserve_thinking if has_prior_reasoning else None),
        reasoning_config=reasoning_config,
    )
    wire_effort = wire_reasoning_effort(reasoning_effort, reasoning_config)
    if wire_effort is not None:
        shape["reasoning_effort"] = wire_effort
    return shape
