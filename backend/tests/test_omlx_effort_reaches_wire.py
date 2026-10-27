"""The reasoning effort a user picks must reach the model.

A discovered level that never leaves CORSIA makes the selector decorative:
the control moves, the model does not. These pin the wire contract.
"""

import asyncio
from unittest.mock import patch

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
    """The level rides in extra_body, where litellm passes it through untouched."""
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


def test_omlx_request_passes_litellm_parameter_validation():
    """Exercise litellm's own allowlist, not a mock of base_completion.

    litellm rejects ``reasoning_effort`` as a top-level parameter for a generic
    OpenAI-compatible model and ``drop_params`` is off, so a top-level level
    would fail every local reasoning turn before any HTTP attempt. Inside
    ``extra_body`` it is merged into the request JSON untouched.
    """
    from unittest.mock import AsyncMock

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
        try:
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
        except RuntimeError:
            pass

    assert sent["extra_body"]["reasoning_effort"] == "medium"
    assert "reasoning_effort" not in {k for k, v in sent.items() if v is not None} - {"extra_body"}
    validated = get_optional_params(
        model=sent["model"],
        custom_llm_provider="openai",
        drop_params=False,
        **{k: sent[k] for k in ("reasoning_effort", "extra_body", "max_tokens") if sent.get(k) is not None},
    )
    assert validated["extra_body"]["reasoning_effort"] == "medium"


_QWEN38 = {"type": "effort_levels", "values": ["xhigh", "medium", "low"], "default": "xhigh"}


def test_omlx_refuses_a_level_outside_the_models_vocabulary():
    """A stale saved level must fail loudly, not be remapped by the server."""
    import pytest
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


def test_omlx_sends_the_managed_level_only_for_an_unset_effort():
    """CORSIA's own oMLX runs at its level unless the thread picks one."""
    managed = {**_QWEN38, "default": "medium", "send_when_unset": "medium"}
    with patch("llm.reasoning_config.reasoning_config_for_model", return_value=managed):
        unset = _dispatch("omlx@srv/qwen38", "omlx", None)
        picked = _dispatch("omlx@srv/qwen38", "omlx", "low")
        auto = _dispatch("omlx@srv/qwen38", "omlx", "auto")
    with patch("llm.reasoning_config.reasoning_config_for_model", return_value=_QWEN38):
        personal = _dispatch("omlx@srv/qwen38", "omlx", None)
    assert unset["extra_body"]["reasoning_effort"] == "medium"
    assert picked["extra_body"]["reasoning_effort"] == "low"
    assert "reasoning_effort" not in (auto.get("extra_body") or {})
    assert "reasoning_effort" not in (personal.get("extra_body") or {})
