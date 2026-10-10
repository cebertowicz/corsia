
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm.inference_params import inference_params_for  # noqa: E402

def _names(fields: list[dict]) -> list[str]:
    return [f["name"] for f in fields]


def _by_name(fields: list[dict], name: str) -> dict | None:
    return next((f for f in fields if f["name"] == name), None)


def test_omlx_fields_full_sampling_kit():
    fields = inference_params_for("omlx", "qwen3-32b")
    expected = {
        "temperature", "top_p", "top_k", "repetition_penalty",
        "repetition_context_size", "frequency_penalty", "presence_penalty",
        "max_tokens",
    }
    assert set(_names(fields)) == expected


def test_omlx_fields_penalty_ranges():
    fields = inference_params_for("omlx", "qwen3-32b")
    for name in ("frequency_penalty", "presence_penalty"):
        f = _by_name(fields, name)
        assert f is not None, f"{name} missing"
        assert f["min"] == -2 and f["max"] == 2
        assert f["type"] == "float"


def test_omlx_static_fallback_marks_silently_dropped_params_as_unsupported():
    fields = inference_params_for("omlx", "qwen3.6-27b-mlx-8bit")
    f = _by_name(fields, "repetition_context_size")
    assert f is not None and f["supported"] is False
    assert "silently drops" in f["unsupported_reason"]
    for name in ("top_k", "repetition_penalty", "temperature", "top_p"):
        f = _by_name(fields, name)
        assert f is not None and f["supported"] is True, name


def test_omlx_with_server_id_uses_live_introspection(monkeypatch):
    from llm import inference_params as inf_module
    monkeypatch.setattr(
        inf_module, "_api_base_for_server",
        lambda sid: "http://stub-server:5200/v1",
    )
    from llm.providers.omlx_introspection import _CACHE, invalidate_cache
    import time
    invalidate_cache()
    _CACHE[("stub-server", 5200)] = (
        time.monotonic(),
        frozenset({
            "model", "messages", "temperature", "top_p", "top_k",
            "max_tokens", "frequency_penalty", "presence_penalty",
            "repetition_penalty", "repetition_context_size",
        }),
    )
    try:
        fields = inference_params_for(
            "omlx", "qwen3.6-27b-mlx-8bit", server_id="ns-fake",
        )
        top_k = _by_name(fields, "top_k")
        assert top_k is not None and top_k["supported"] is True
        min_p = _by_name(fields, "min_p")
        temp = _by_name(fields, "temperature")
        assert temp is not None and temp["supported"] is True
    finally:
        invalidate_cache()


def test_only_omlx_has_server_settings():
    """Sampling is the provider setting; no other row gets a CORSIA editor."""
    for provider in ("openai", "anthropic", "external", "nonsense"):
        assert inference_params_for(provider, "anything") == []


def test_field_descriptors_have_required_shape():
    required = {"name", "label", "type", "min", "max", "step", "supported"}
    for provider, mid in [("omlx", "any")]:
        for f in inference_params_for(provider, mid):
            missing = required - f.keys()
            assert not missing, f"{provider}/{mid} field {f['name']} missing {missing}"
