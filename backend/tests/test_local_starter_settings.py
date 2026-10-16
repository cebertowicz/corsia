from __future__ import annotations

import asyncio

from llm import local_setup
from llm.local_model import LocalModel
from llm.local_profiles import OMLX_GLOBAL_SETTINGS


def test_canonical_setup_settings_use_exact_upstream_payloads_and_no_environment_override(monkeypatch):
    calls: list[tuple[str, str, dict]] = []
    model = LocalModel("Jundot/gemma-4-26B-A4B-it-oQ3.5e-mtp", 14_450_611_912, 90_890)

    class Response:
        status_code = 200

        def json(self):
            return {
                "runtime_applied": ["max_concurrent_requests", "prefill_priority", "skip_api_key_verification"],
                "models": [{"id": model.model, "thinking_default": False}],
            }

    async def admin_request(_cfg, method, path, *, json=None, timeout=None):
        calls.append((method, path, json))
        return Response()

    monkeypatch.setattr("llm.providers.omlx_settings.admin_request", admin_request)
    monkeypatch.setattr("llm.providers.omlx_endpoints.admin_request", admin_request)

    asyncio.run(local_setup._configure_global("server"))
    asyncio.run(local_setup._configure_model_settings("server", model))

    assert calls == [
        ("POST", "/global-settings", {
            "max_concurrent_requests": 1, "prefill_priority": "context",
            "skip_api_key_verification": True,
        }),
        ("GET", "/models", None),
        # No sampling: the model's own generation config applies. Gemma 4 thinks only when asked.
        ("PUT", f"/models/{model.model}/settings", {
            "max_context_window": 90_890, "max_tokens": 16384,
            "enable_thinking": True, "mtp_enabled": True,
            "turboquant_kv_enabled": True,
            "turboquant_kv_bits": 8, "turboquant_skip_last": True,
            "is_default": True,
        }),
    ]
    assert "OMLX_SDPA256_TILED" not in str(calls)
    # The one auth write CORSIA makes: no bind host, port, CORS or key setting.
    assert not {"host", "port", "cors_origins", "api_key", "server"} & set(calls[0][2])
    assert not {"host", "port", "cors_origins", "api_key", "server"} & set(OMLX_GLOBAL_SETTINGS)


def test_setup_serializes_requests_as_the_setup_decision_says():
    # setup_strings.OMLX ("At {recommended}, oMLX finishes one request before it starts the
    # next") describes this value; changing it makes that decision text false.
    assert OMLX_GLOBAL_SETTINGS["max_concurrent_requests"] == 1
