"""The reasoning effort CORSIA resolves must reach an oMLX model.

A discovered level that never leaves CORSIA makes the selector decorative; these
pin the router/wire contract and the managed-default dispatch path.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from llm import router


def _dispatch(model, api_key, effort):
    """Return the kwargs router hands to base_completion for one request."""
    captured = {}

    async def fake_base_completion(messages, tools, **kwargs):
        captured.update(kwargs)
        from llm.router import LLMResponse
        return LLMResponse(content="ok")

    with patch("llm.base.base_completion", fake_base_completion):
        asyncio.run(
            router.chat_completion(
                [{"role": "user", "content": "hi"}],
                None,
                model=model,
                api_key=api_key,
                reasoning_effort=effort,
            )
        )
    return captured


def test_omlx_sends_the_selected_level_as_reasoning_effort():
    """The level rides in extra_body, where LiteLLM passes it through untouched."""
    captured = _dispatch("omlx@srv/qwen38", "omlx", "xhigh")
    assert captured["extra_body"]["reasoning_effort"] == "xhigh"
    assert captured["reasoning_effort"] is None


def test_omlx_never_sends_a_thinking_budget():
    """The budget is a truncation ceiling oMLX owns per model, not a depth control."""
    captured = _dispatch("omlx@srv/qwen38", "omlx", "high")
    assert "thinking_budget" not in (captured.get("extra_body") or {})


def test_omlx_auto_omits_the_field_so_the_server_default_applies():
    captured = _dispatch("omlx@srv/qwen38", "omlx", "auto")
    assert captured["reasoning_effort"] is None
    assert "reasoning_effort" not in (captured.get("extra_body") or {})


def test_omlx_none_turns_thinking_off_rather_than_sending_a_level():
    captured = _dispatch("omlx@srv/qwen38", "omlx", "none")
    assert captured["reasoning_effort"] is None
    assert captured["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False


def test_non_omlx_providers_are_untouched():
    """The effort field is scoped to oMLX; other providers keep their own path."""
    captured = _dispatch("some-local/model", "not-omlx", "high")
    assert captured["reasoning_effort"] is None


_QWEN38 = {"type": "effort_levels", "values": ["xhigh", "medium", "low"], "default": "xhigh"}


def test_omlx_request_passes_litellm_parameter_validation():
    """Use LiteLLM's allowlist, not only a mock of base_completion.

    LiteLLM rejects a top-level reasoning_effort for a generic OpenAI-compatible
    model when drop_params is off. oMLX needs the level in extra_body instead.
    """
    from litellm.utils import get_optional_params

    sent: dict = {}

    async def fake_acompletion(**kwargs):
        sent.update(kwargs)
        raise RuntimeError("stop before HTTP")

    with (
        patch("llm.base.acompletion", fake_acompletion),
        patch("llm.base._omlx_loaded_only_preflight", AsyncMock(return_value=None)),
        patch("llm.reasoning_config.reasoning_config_for_model", return_value=_QWEN38),
    ):
        asyncio.run(
            router.chat_completion(
                [{"role": "user", "content": "hi"}],
                None,
                model="Qwen3.8-Flash-Next-MLX-oQ8-MTP",
                api_key="omlx",
                api_base="http://127.0.0.1:5200/v1",
                reasoning_effort="medium",
                retry_transient_errors=False,
            )
        )

    assert sent["extra_body"]["reasoning_effort"] == "medium"
    assert "reasoning_effort" not in {key for key, value in sent.items() if value is not None} - {"extra_body"}
    validated = get_optional_params(
        model=sent["model"],
        custom_llm_provider="openai",
        drop_params=False,
        **{
            key: sent[key]
            for key in ("reasoning_effort", "extra_body", "max_tokens")
            if sent.get(key) is not None
        },
    )
    assert validated["extra_body"]["reasoning_effort"] == "medium"


def test_omlx_refuses_a_level_outside_the_models_vocabulary():
    """A stale saved level must fail loudly, not be remapped by the server."""
    from llm.providers.omlx_thinking import UnsupportedReasoningEffortError

    with patch("llm.reasoning_config.reasoning_config_for_model", return_value=_QWEN38):
        with pytest.raises(UnsupportedReasoningEffortError):
            _dispatch("omlx@srv/qwen38", "omlx", "high")


def test_omlx_forwards_a_level_inside_the_models_vocabulary():
    with patch("llm.reasoning_config.reasoning_config_for_model", return_value=_QWEN38):
        captured = _dispatch("omlx@srv/qwen38", "omlx", "medium")
    assert captured["extra_body"]["reasoning_effort"] == "medium"
    assert captured["reasoning_effort"] is None


def test_omlx_sends_no_level_to_a_toggle_only_model_but_asserts_thinking_on():
    toggle = {"type": "boolean", "default": True}
    with patch("llm.reasoning_config.reasoning_config_for_model", return_value=toggle):
        captured = _dispatch("omlx@srv/qwen36", "omlx", "medium")
    assert captured["reasoning_effort"] is None
    assert "reasoning_effort" not in captured["extra_body"]
    assert captured["extra_body"]["chat_template_kwargs"]["enable_thinking"] is True


@pytest.mark.asyncio
async def test_new_managed_omlx_thread_sends_medium_for_an_unset_effort(monkeypatch):
    from core.turn_core import TurnIdentity
    from infra.send_dispatch import dispatch_native_ws
    from llm.resolution import ExecutionTarget

    captured: dict[str, str | None] = {}

    async def fake_execute_turn(*, reasoning_effort, start_ack, **_kwargs):
        captured["reasoning_effort"] = reasoning_effort
        start_ack.set_result(TurnIdentity("turn-managed-medium", 1))

    managed_shape = {
        "type": "effort_levels",
        "values": ["low", "medium", "xhigh"],
        "default": "medium",
        "applies_default_when_unset": True,
    }
    monkeypatch.setattr(
        "llm.reasoning_config.reasoning_config_for_model",
        lambda _model: managed_shape,
    )
    monkeypatch.setattr("ws.turn.execute_turn", fake_execute_turn)

    target = ExecutionTarget(
        catalog_id="omlx@corsia-server/qwen",
        harness="native",
        provider="omlx",
        backend="network",
        transport_model="omlx@corsia-server/qwen",
        expected_reported_model=None,
        context_window=262144,
        available=True,
        availability_reason=None,
        display_name="Qwen",
        supports_reasoning=True,
    )

    await dispatch_native_ws(
        "thread-managed-medium",
        {"reasoning_effort": None},
        "hello",
        {"id": "user-1"},
        object(),
        target=target,
        turn_id="turn-managed-medium",
        attachments=[],
        is_retry=False,
        reasoning_effort=None,
    )

    assert captured["reasoning_effort"] == "medium"
