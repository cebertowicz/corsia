from __future__ import annotations

import asyncio

from llm import local_setup


def test_canonical_setup_settings_use_exact_upstream_payloads_and_no_environment_override(monkeypatch):
    calls: list[tuple[str, str, dict]] = []

    class Response:
        status_code = 200

        def json(self):
            return {"runtime_applied": ["max_concurrent_requests", "prefill_priority", "skip_api_key_verification"]}

    async def admin_request(_cfg, method, path, *, json=None, timeout=None):
        calls.append((method, path, json))
        return Response()

    monkeypatch.setattr("llm.providers.omlx_settings.admin_request", admin_request)
    monkeypatch.setattr("llm.providers.omlx_endpoints.admin_request", admin_request)

    asyncio.run(local_setup._configure_global("server"))
    asyncio.run(local_setup._configure_model_settings("server", 32768, local_setup.DEFAULT_PROFILE))

    assert calls == [
        ("POST", "/global-settings", {
            "max_concurrent_requests": 1, "prefill_priority": "context",
            "skip_api_key_verification": True,
        }),
        ("PUT", f"/models/{local_setup.DEFAULT_PROFILE.model}/settings", {
            "max_context_window": 32768, "max_tokens": 16384,
            "temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.0,
            "presence_penalty": 0.0, "repetition_penalty": 1.0,
            "enable_thinking": True,
            "chat_template_kwargs": {"reasoning_effort": "medium"},
            "turboquant_kv_enabled": True,
            "turboquant_kv_bits": 8, "turboquant_skip_last": True,
            "is_default": True,
        }),
    ]
    assert "OMLX_SDPA256_TILED" not in str(calls)
    # The one auth write CORSIA makes: no bind host, port, CORS or key setting.
    assert not {"host", "port", "cors_origins", "api_key", "server"} & set(calls[0][2])
    assert not {"host", "port", "cors_origins", "api_key", "server"} & set(local_setup.OMLX_GLOBAL_SETTINGS)


def test_starter_serializes_requests_as_the_setup_decision_says():
    # setup_strings.OMLX ("At {recommended}, oMLX finishes one request before it starts the
    # next") describes this value; changing it makes that decision text false.
    assert local_setup.OMLX_GLOBAL_SETTINGS["max_concurrent_requests"] == 1
